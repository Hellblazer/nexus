// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.datamove;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static org.assertj.core.api.Assertions.assertThat;

import dev.nexus.service.PgCatalogProbes;
import dev.nexus.service.db.CollectionTupleLock;
import dev.nexus.service.db.DrivingTableIsolationCheck;
import org.jooq.Field;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.jooq.impl.SQLDataType;
import org.junit.jupiter.api.Test;
import java.sql.Connection;
import java.sql.DriverManager;
import java.util.ArrayList;
import java.util.TreeMap;
import java.util.TreeSet;
import java.util.List;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.TimeUnit;
import java.util.Map;

/**
 * RDR-229 (Sam 2026-11-21, "Owner is the source"; bead nexus-p2m1x.14): the registered data move
 * {@code rdr229-reown-repo-stems-from-curator} (vectors-067-1), run by the real {@link DataMoveWorker} with the real
 * {@link VerifyingFinisher} on the pg_background image, over the real {@code nexus.catalog_collections} and
 * {@code nexus.catalog_owners} (FORCE row-level security, the migrating role bound by it).
 *
 * <p>The move is registered by running the shipped changeset file through Liquibase, never by a copy of its text, so what
 * runs here is what ships. It is ONE statement with a data-modifying CTE (lock, INSERT of the missing owners, UPDATE); this
 * class is also the proof that the worker runs such a template to completion.
 *
 * <p>The admin role reads every tenant's {@code catalog_collections} rows (the worker's tenant list, the table-wide
 * verification) through vectors-065's owner-read policy {@code catalog_collections_owner_read} (RDR-229 D2); the move's own
 * changeset carries none. {@code theRealOwnerReadPolicyIsApplied} pins that the shipped policy, not a test fixture, is in place.
 *
 * <p><b>Where an operator looks for a skipped row.</b> {@code SELECT collection, details FROM nexus.gc_audit WHERE operation =
 * 'rdr229_reown_skipped'}, read with the tenant set (gc_audit is tenant-isolated). {@code details} carries {@code reason},
 * {@code owner_id}, {@code destination_owner_id}, {@code content_type} and {@code held_by}. The template writes exactly two
 * reasons: {@code 'destination tuple held by <name>'} (another live collection already holds the destination tuple) and
 * {@code 'destination tuple claimed in this run by <name>'} (the row sorts after {@code <name>} among rows that would move onto one
 * free tuple). The verification requires each report: an occupied row passes only with its gc_audit row, so a skip is never
 * silent ({@code anOccupiedRowWithNoReport_blocksCompletion_soASkipIsNeverSilent}); the occupied and claimed cases are the
 * restored-legacy test and the two same-hash tests (one chunk, then chunk_size 1).
 *
 * <p>Falsifiers, by hand: drop the {@code lck} CTE or its {@code CROSS JOIN lck} and the lock test sees the move complete
 * while the lock is held; make {@code repo_owner} read only {@code ins} and the existing-owner test moves nothing; drop the
 * repo-owner arm of {@code cand} and the docs-only-with-an-owner and chunk-straddle tests leave rows on the curator; drop
 * the {@code skipped} CTE's use in the UPDATE and the occupied-destination test moves the legacy row onto its native twin's
 * tuple; drop the {@code reported} CTE and the verification blocks the move.
 */
class Rdr229ReownRepoStemsMoveIntegrationTest extends Rdr229ReownFixtures {

    @Test
    void theRealOwnerReadPolicyIsApplied() {
        assertThat(PgCatalogProbes.policyExists(ctx(), "nexus", "catalog_collections", "catalog_collections_owner_read"))
            .as("vectors-065's owner-read policy is applied by the product schema").isTrue();
    }

    // ── the stop-point: a CTE template, run by the worker to completion ─────────────────────────────────────

    @Test
    void gateXr789Shape_theRepoStemIsReownedToANewRepoOwner_theDocCorpusStays() throws Exception {
        gateXr789(T);
        collection(T, "knowledge__distributed-systems__voyage-context-3__v1", "1-40");
        collection(T, "quarantine-code__arcaneum-2ad2825c__voyage-code-3__v1", "1-40", "quarantine", "");
        collection(T, "code__arcaneum-2ad2825c__voyage-code-3__v0", "1-40", "live", "code__arcaneum-2ad2825c__voyage-code-3__v1");
        register();
        assertThat(ownersOf(T)).as("non-vacuity: no arcaneum owner before the move").containsExactly("1.40=knowledge/curator//");

        runToCompletion();

        assertThat(ownersOf(T)).as("one new repo owner, the next number, repo_hash = the stem's hash8, empty root")
            .containsExactly("1.40=knowledge/curator//", "1.41=arcaneum/repo/2ad2825c/");
        assertThat(ownerOf(T, ARC_CODE)).isEqualTo("1-41");
        assertThat(ownerOf(T, ARC_DOCS)).isEqualTo("1-41");
        assertThat(ownerOf(T, WOW_DOCS)).as("a doc corpus keeps its curator").isEqualTo("1-40");
        assertThat(ownerOf(T, "knowledge__distributed-systems__voyage-context-3__v1")).as("knowledge keeps its subject's owner")
            .isEqualTo("1-40");
        assertThat(ownerOf(T, "quarantine-code__arcaneum-2ad2825c__voyage-code-3__v1"))
            .as("the origin's quarantine sibling moves with it: QuarantineOrigin.rowsOf matches an untagged sibling by owner_id")
            .isEqualTo("1-41");
        assertThat(ownerOf(T, "code__arcaneum-2ad2825c__voyage-code-3__v0")).as("a superseded row is not touched")
            .isEqualTo("1-40");
    }

    // ── what is a repo ──────────────────────────────────────────────────────────────────────────────────────

    @Test
    void aDocsOnlyHexSuffixedCorpus_isNeverTouched_andNoOwnerIsInserted() throws Exception {
        owner(T, "1.40", "knowledge", "curator", null);
        collection(T, "docs__notes-0badc0de__voyage-context-3__v1", "1-40");
        collection(T, "rdr__notes-0badc0de__voyage-context-3__v1", "1-40");
        register();

        runToCompletion();

        assertThat(ownerOf(T, "docs__notes-0badc0de__voyage-context-3__v1")).isEqualTo("1-40");
        assertThat(ownerOf(T, "rdr__notes-0badc0de__voyage-context-3__v1")).isEqualTo("1-40");
        assertThat(ownersOf(T)).as("no code__ row, so no repo and no owner").containsExactly("1.40=knowledge/curator//");
    }

    @Test
    void aStemOfTheRepoShapeWithANonHexSuffix_orAnOwnerThatIsNotACurator_isNotARepoToThisMove() throws Exception {
        owner(T, "1.40", "knowledge", "curator", null);
        owner(T, "1.9", "some-repo", "repo", "11112222");
        collection(T, "code__arcaneum-2AD2825C__voyage-code-3__v1", "1-40");      // upper-case hex is not hash8
        collection(T, "code__shorthash-2ad282__voyage-code-3__v1", "1-40");       // six digits
        collection(T, "code__other-33334444__voyage-code-3__v1", "1-9");          // a repo owner of another hash
        collection(T, "docs__other-33334444__voyage-context-3__v1", "1-40");
        register();

        runToCompletion();

        assertThat(ownerOf(T, "code__arcaneum-2AD2825C__voyage-code-3__v1")).isEqualTo("1-40");
        assertThat(ownerOf(T, "code__shorthash-2ad282__voyage-code-3__v1")).isEqualTo("1-40");
        assertThat(ownerOf(T, "code__other-33334444__voyage-code-3__v1")).isEqualTo("1-9");
        assertThat(ownerOf(T, "docs__other-33334444__voyage-context-3__v1")).as("its code row is neither curator-owned nor hash-owned")
            .isEqualTo("1-40");
        assertThat(ownersOf(T)).hasSize(2);
    }

    // ── the owner ───────────────────────────────────────────────────────────────────────────────────────────

    @Test
    void anExistingRepoOwnerWithThatHash_isReusedNotDuplicated() throws Exception {
        owner(T, "1.7", "arcaneum-checkout", "repo", "2ad2825c");   // gateXr789 seeds the curator 1.40
        gateXr789(T);
        register();

        runToCompletion();

        assertThat(ownerOf(T, ARC_CODE)).isEqualTo("1-7");
        assertThat(ownerOf(T, ARC_DOCS)).isEqualTo("1-7");
        assertThat(ownerOf(T, WOW_DOCS)).isEqualTo("1-40");
        assertThat(ownersOf(T)).as("no owner inserted").containsExactly("1.40=knowledge/curator//",
            "1.7=arcaneum-checkout/repo/2ad2825c/");
    }

    @Test
    void aNameCollisionOnTheSlug_getsTheSlugAndHash_andTwoNewStemsOfOneSlugGetConsecutiveNumbers() throws Exception {
        owner(T, "1.40", "knowledge", "curator", null);
        owner(T, "1.12", "taken", "repo", "99998888");
        collection(T, "code__taken-2ad2825c__voyage-code-3__v1", "1-40");        // slug "taken" is held by 1.12
        collection(T, "code__twin-aaaaaaaa__voyage-code-3__v1", "1-40");         // two new stems, one slug
        collection(T, "code__twin-bbbbbbbb__voyage-code-3__v1", "1-40");
        collection(T, "rdr__twin-bbbbbbbb__voyage-context-3__v1", "1-40");
        register();

        runToCompletion();

        assertThat(ownersOf(T)).containsExactly("1.12=taken/repo/99998888/", "1.40=knowledge/curator//",
            "1.41=taken-2ad2825c/repo/2ad2825c/", "1.42=twin/repo/aaaaaaaa/", "1.43=twin-bbbbbbbb/repo/bbbbbbbb/");
        assertThat(ownerOf(T, "code__taken-2ad2825c__voyage-code-3__v1")).isEqualTo("1-41");
        assertThat(ownerOf(T, "code__twin-aaaaaaaa__voyage-code-3__v1")).isEqualTo("1-42");
        assertThat(ownerOf(T, "code__twin-bbbbbbbb__voyage-code-3__v1")).isEqualTo("1-43");
        assertThat(ownerOf(T, "rdr__twin-bbbbbbbb__voyage-context-3__v1")).isEqualTo("1-43");
    }

    // ── tenants, chunks, reruns ─────────────────────────────────────────────────────────────────────────────

    @Test
    void tenantsAreIndependent_eachHasItsOwnNumbering_andOneTenantsRowsNeverMoveToAnothersOwner() throws Exception {
        gateXr789(T);
        owner(U, "1.3", "knowledge", "curator", null);
        owner(U, "1.4", "older-repo", "repo", "cafe0001");
        collection(U, ARC_CODE, "1-3");
        collection(U, ARC_DOCS, "1-3");
        register();

        runToCompletion();

        assertThat(ownerOf(T, ARC_CODE)).isEqualTo("1-41");
        assertThat(ownerOf(U, ARC_CODE)).as("tenant U numbers from its own owners").isEqualTo("1-5");
        assertThat(ownerOf(U, ARC_DOCS)).isEqualTo("1-5");
        assertThat(ownersOf(U)).containsExactly("1.3=knowledge/curator//", "1.4=older-repo/repo/cafe0001/",
            "1.5=arcaneum/repo/2ad2825c/");
        assertThat(ownersOf(T)).containsExactly("1.40=knowledge/curator//", "1.41=arcaneum/repo/2ad2825c/");
    }

    @Test
    void aStemWhoseRowsFallInDifferentChunks_isReownedWhole_whatEverTheChunkSize() throws Exception {
        gateXr789(T);
        collection(T, "rdr__arcaneum-2ad2825c__voyage-context-3__v1", "1-40");
        register();
        ctx().update(table("nexus", "data_moves"))
            .set(DSL.field(DSL.name("chunk_size"), Integer.class), 1)   // every collection is a chunk of its own
            .where(DSL.field(DSL.name("label"), String.class).eq(LABEL)).execute();

        runToCompletion();

        assertThat(progress(LABEL).total()).as("non-vacuity: several chunks for tenant " + T).isGreaterThanOrEqualTo(4);
        assertThat(ownerOf(T, ARC_CODE)).isEqualTo("1-41");
        assertThat(ownerOf(T, ARC_DOCS)).as("the docs row sorts after the code row, in a later chunk").isEqualTo("1-41");
        assertThat(ownerOf(T, "rdr__arcaneum-2ad2825c__voyage-context-3__v1")).isEqualTo("1-41");
        assertThat(ownerOf(T, WOW_DOCS)).isEqualTo("1-40");
        assertThat(ownersOf(T)).as("one owner, however many chunks").containsExactly("1.40=knowledge/curator//",
            "1.41=arcaneum/repo/2ad2825c/");
    }

    @Test
    void runningTheTemplateAgain_changesNothing() throws Exception {
        gateXr789(T);
        register();
        runToCompletion();
        List<String> owners = ownersOf(T);
        var before = ctx().select(CATALOG_COLLECTIONS.NAME, CATALOG_COLLECTIONS.OWNER_ID).from(CATALOG_COLLECTIONS)
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(T)).orderBy(CATALOG_COLLECTIONS.NAME).fetch().formatCSV();

        // the same template under a new label: a fresh dml_utils run executes it again over the moved rows
        // Read the shipped row directly: DataMoveRegistry.reload answers only an open move, and this one completed.
        var first = ctx().select(DSL.field(DSL.name("template"), String.class),
                DSL.field(DSL.name("verification"), String.class), DSL.field(DSL.name("chunk_size"), Integer.class))
            .from(table("nexus", "data_moves")).where(DSL.field(DSL.name("label"), String.class).eq(LABEL)).fetchOne();
        assertThat(first).as("the shipped move row").isNotNull();
        DataMoveTestFixtures.registerOn(ctx(), "again", "nexus", "catalog_collections", first.value3(), 0, null,
            "stamp-template.sql");
        ctx().update(table("nexus", "data_moves"))
            .set(DSL.field(DSL.name("template"), String.class), first.value1())
            .set(DSL.field(DSL.name("verification"), String.class), first.value2())
            .where(DSL.field(DSL.name("label"), String.class).eq("again")).execute();
        for (int i = 0; i < 3 && completedAt("again") == null; i++) {
            worker(installed(), null, FailureHandler.BLOCK_ALWAYS, new VerifyingFinisher()).runCycle();
        }
        assertThat(completedAt("again")).as("the second run completed").isNotNull();

        assertThat(ownersOf(T)).isEqualTo(owners);
        assertThat(ctx().select(CATALOG_COLLECTIONS.NAME, CATALOG_COLLECTIONS.OWNER_ID).from(CATALOG_COLLECTIONS)
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(T)).orderBy(CATALOG_COLLECTIONS.NAME).fetch().formatCSV())
            .isEqualTo(before);
    }

    @Test
    void aFreshInstall_withNothingToReown_completesOnTheFirstWorkerPass() throws Exception {
        register();

        int moved = worker(installed(), null, FailureHandler.BLOCK_ALWAYS, new VerifyingFinisher()).runCycle();

        assertThat(moved).as("the worker worked the move").isEqualTo(1);
        assertThat(completedAt(LABEL)).as("an empty catalog completes in one pass, so boot 1 completes it").isNotNull();
    }

    // ── a repo owner that already exists is a repo signal, whatever the stem's rows are ─────────────────────

    @Test
    void aDocsOnlyStemUnderACurator_whoseHashHasARepoOwner_isReowned_butARowOfAnotherOwnerIsNot() throws Exception {
        owner(T, "1.40", "knowledge", "curator", null);
        owner(T, "1.7", "arcaneum-checkout", "repo", "2ad2825c");
        owner(T, "1.9", "some-repo", "repo", "11112222");
        collection(T, ARC_DOCS, "1-40");                                            // no code__ row at all
        collection(T, "rdr__arcaneum-2ad2825c__voyage-context-3__v1", "1-9");     // another repo owner's: not a curator row
        collection(T, "docs__notes-0badc0de__voyage-context-3__v1", "1-40");     // docs-only hex stem, no owner of that hash
        register();

        runToCompletion();

        assertThat(ownerOf(T, ARC_DOCS)).as("the stem's hash has a repo owner already: a docs-only stem is that repo's")
            .isEqualTo("1-7");
        assertThat(ownerOf(T, "rdr__arcaneum-2ad2825c__voyage-context-3__v1")).as("a row on a non-curator owner is left alone")
            .isEqualTo("1-9");
        assertThat(ownerOf(T, "docs__notes-0badc0de__voyage-context-3__v1")).as("no owner of that hash and no code__ row: a corpus")
            .isEqualTo("1-40");
        assertThat(ownersOf(T)).as("no owner inserted").hasSize(3);
    }

    @Test
    void aRestoredLegacyRowBesideItsNativeTwin_isSkippedAndReportedOnce_theOthersMove_andNoTupleHoldsTwoLiveRows()
            throws Exception {
        restoredLegacyBesideNative(T);
        register();
        ctx().update(table("nexus", "data_moves"))
            .set(DSL.field(DSL.name("chunk_size"), Integer.class), 1)   // several chunks: the report must not repeat
            .where(DSL.field(DSL.name("label"), String.class).eq(LABEL)).execute();

        runToCompletion();

        assertThat(progress(LABEL).total()).as("non-vacuity: several chunks").isGreaterThanOrEqualTo(4);
        assertThat(ownerOf(T, ARC_CODE)).as("the legacy row stays on the curator, alone in its own tuple").isEqualTo("1-40");
        assertThat(ownerOf(T, "code__1-17__voyage-code-3__v1")).as("the native row is untouched").isEqualTo("1-17");
        assertThat(ownerOf(T, ARC_DOCS)).as("a row whose destination is free moves").isEqualTo("1-17");
        assertThat(ownerOf(T, "quarantine-" + ARC_CODE)).as("a skipped origin's sibling stays with it").isEqualTo("1-40");
        assertThat(ownerOf(T, "quarantine-" + ARC_DOCS)).as("a moved origin's sibling moves with it").isEqualTo("1-17");
        assertThat(ownersOf(T)).as("no owner inserted").containsExactly("1.17=arcaneum/repo/2ad2825c/",
            "1.40=knowledge/curator//");
        var reports = skipReports(T);
        assertThat(reports).as("one report row for the one skipped row, however many chunks ran").hasSize(1);
        assertThat(reports.get(0)).startsWith(ARC_CODE + " ");
        assertThat(skipReasons(T)).as("the held-by text is pinned: the reason names the native row that holds the tuple")
            .isEqualTo(Map.of(ARC_CODE, "destination tuple held by code__1-17__voyage-code-3__v1"));
        var twins = ctx().select(CATALOG_COLLECTIONS.CONTENT_TYPE, CATALOG_COLLECTIONS.OWNER_ID,
                CATALOG_COLLECTIONS.EMBEDDING_MODEL, CATALOG_COLLECTIONS.MODEL_VERSION, DSL.count())
            .from(CATALOG_COLLECTIONS)
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(T)).and(CATALOG_COLLECTIONS.LIFECYCLE_STATE.eq("live"))
            .and(CATALOG_COLLECTIONS.SUPERSEDED_BY.eq(""))
            .groupBy(CATALOG_COLLECTIONS.CONTENT_TYPE, CATALOG_COLLECTIONS.OWNER_ID, CATALOG_COLLECTIONS.EMBEDDING_MODEL,
                CATALOG_COLLECTIONS.MODEL_VERSION)
            .having(DSL.count().gt(1)).fetch();
        assertThat(twins).as("P3.8's partial UNIQUE shape: at most one live row per tuple").isEmpty();
    }

    private void twoStemsOfOneHash(int chunkSize) throws Exception {
        twoStemsRegistered(chunkSize);

        runToCompletion();

        assertThat(ownersOf(T)).as("one owner per hash, however many stems carry it, and no tumbler consumed by a dropped "
                + "duplicate (DISTINCT ON (hash8) in fresh): 1.41 for c0ffee11 and 1.42, not 1.43, for the next hash")
            .containsExactly("1.40=knowledge/curator//", "1.41=a/repo/c0ffee11/", "1.42=c/repo/deadbeef/");
        assertThat(ownerOf(T, "code__c-deadbeef__voyage-code-3__v1")).isEqualTo("1-42");
        assertThat(ownerOf(T, "code__a-c0ffee11__voyage-code-3__v1")).as("the first by name moves").isEqualTo("1-41");
        assertThat(ownerOf(T, "code__b-c0ffee11__voyage-code-3__v1")).as("the other is skipped, alone in its tuple")
            .isEqualTo("1-40");
        assertThat(ownerOf(T, "quarantine-code__b-c0ffee11__voyage-code-3__v1")).as("a skipped origin's sibling stays")
            .isEqualTo("1-40");
        var reports = skipReports(T);
        assertThat(reports).as("the skipped row is reported once, however many chunks ran").hasSize(1);
        assertThat(reports.get(0)).startsWith("code__b-c0ffee11__voyage-code-3__v1 ")
            .contains("destination tuple claimed in this run by code__a-c0ffee11__voyage-code-3__v1");
        var twins = ctx().select(CATALOG_COLLECTIONS.CONTENT_TYPE, CATALOG_COLLECTIONS.OWNER_ID,
                CATALOG_COLLECTIONS.EMBEDDING_MODEL, CATALOG_COLLECTIONS.MODEL_VERSION, DSL.count())
            .from(CATALOG_COLLECTIONS)
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(T)).and(CATALOG_COLLECTIONS.LIFECYCLE_STATE.eq("live"))
            .and(CATALOG_COLLECTIONS.SUPERSEDED_BY.eq(""))
            .groupBy(CATALOG_COLLECTIONS.CONTENT_TYPE, CATALOG_COLLECTIONS.OWNER_ID, CATALOG_COLLECTIONS.EMBEDDING_MODEL,
                CATALOG_COLLECTIONS.MODEL_VERSION)
            .having(DSL.count().gt(1)).fetch();
        assertThat(twins).as("at most one live row per (content_type, owner_id, model, version)").isEmpty();
    }

    @Test
    void twoRowsClaimingOneFreeTuple_inOneChunk_theFirstByNameMoves_theOtherIsSkippedAndReported() throws Exception {
        twoStemsOfOneHash(1000);
        assertThat(progress(LABEL).total()).as("non-vacuity: one chunk covers the tenant's three rows").isLessThan(3);
    }

    @Test
    void twoRowsClaimingOneFreeTuple_inSeparateChunks_theFirstByNameMoves_theOtherIsSkippedAndReported() throws Exception {
        twoStemsOfOneHash(1);
        assertThat(progress(LABEL).total()).as("non-vacuity: every collection is a chunk of its own")
            .isGreaterThanOrEqualTo(3);
    }

    /** Only the occupied legacy row: a move that does nothing leaves it curator-owned, so the verification alone decides. */
    private void occupiedLegacyOnlyWithAMoveThatDoesNothing(String tenant) throws Exception {
        owner(tenant, "1.40", "knowledge", "curator", null);
        owner(tenant, "1.17", "arcaneum", "repo", "2ad2825c");
        collection(tenant, "code__1-17__voyage-code-3__v1", "1-17");
        collection(tenant, ARC_CODE, "1-40");
        register();
        ctx().update(table("nexus", "data_moves"))
            .set(DSL.field(DSL.name("template"), String.class),
                "UPDATE <driving_table> SET owner_id = t.owner_id WHERE <chunking_clause>")
            .where(DSL.field(DSL.name("label"), String.class).eq(LABEL)).execute();
    }

    @Test
    void anOccupiedRowWithNoReport_blocksCompletion_soASkipIsNeverSilent() throws Exception {
        occupiedLegacyOnlyWithAMoveThatDoesNothing(T);

        var w = worker(installed(), null, FailureHandler.BLOCK_ALWAYS, new VerifyingFinisher());
        w.runCycle();

        assertThat(completedAt(LABEL)).as("the verification counts the unreported skip").isNull();
        assertThat(w.states().state(LABEL).orElseThrow().kind()).isEqualTo(MoveStates.Kind.BLOCKED);
    }

    @Test
    void anOccupiedRowWithItsReport_passesTheVerification() throws Exception {
        occupiedLegacyOnlyWithAMoveThatDoesNothing(T);
        ctx().insertInto(table("nexus", "gc_audit"), DSL.field(DSL.name("tenant_id"), String.class),
                DSL.field(DSL.name("operation"), String.class), DSL.field(DSL.name("collection"), String.class))
            .values(T, "rdr229_reown_skipped", ARC_CODE).execute();

        runToCompletion();

        assertThat(ownerOf(T, ARC_CODE)).isEqualTo("1-40");
    }

    @Test
    void aBlankVersionLegacyRowBesideItsV1Twin_isSkippedAsHeld_whenTheMoveRunsBeforeD2() throws Exception {
        blankVersionLegacyBesideNative(T);
        register();

        runToCompletion();

        assertThat(ownerOf(T, ARC_CODE)).as("the blank-version row stays on the curator: D2 would make it v1 on the repo tuple")
            .isEqualTo("1-40");
        assertThat(skipReasons(T)).as("reported, held by the native v1 row")
            .isEqualTo(Map.of(ARC_CODE, "destination tuple held by code__1-17__voyage-code-3__v1"));
        assertThat(ctx().select(CATALOG_COLLECTIONS.MODEL_VERSION).from(CATALOG_COLLECTIONS)
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(T)).and(CATALOG_COLLECTIONS.NAME.eq(ARC_CODE))
            .fetchOne(CATALOG_COLLECTIONS.MODEL_VERSION)).as("non-vacuity: D2 has not run, the stored version is blank").isEmpty();
    }

    // ── S3: one definition of held: unsuperseded and not in quarantine ──────────────────────────────────────

    @Test
    void aDormantHolder_holdsItsTuple_soTheLegacyRowIsSkippedAndReported() throws Exception {
        owner(T, "1.40", "knowledge", "curator", null);
        owner(T, "1.17", "arcaneum", "repo", "2ad2825c");
        collection(T, "code__1-17__voyage-code-3__v1", "1-17", "dormant", "");
        collection(T, ARC_CODE, "1-40");
        register();

        runToCompletion();

        assertThat(ownerOf(T, ARC_CODE)).isEqualTo("1-40");
        assertThat(skipReasons(T)).isEqualTo(Map.of(ARC_CODE, "destination tuple held by code__1-17__voyage-code-3__v1"));
    }

    @Test
    void aHolderInQuarantine_doesNotHold_soTheLegacyRowMoves() throws Exception {
        owner(T, "1.40", "knowledge", "curator", null);
        owner(T, "1.17", "arcaneum", "repo", "2ad2825c");
        collection(T, "code__1-17__voyage-code-3__v1", "1-17", "quarantine", "");
        collection(T, ARC_CODE, "1-40");
        register();

        runToCompletion();

        assertThat(ownerOf(T, ARC_CODE)).as("a row in quarantine is outside the P3.8 index, so it holds nothing").isEqualTo("1-17");
        assertThat(skipReasons(T)).isEmpty();
    }

    @Test
    void aDormantRowOfARepoStem_isReownedLikeALiveOne() throws Exception {
        gateXr789(T);
        ctx().update(CATALOG_COLLECTIONS).set(CATALOG_COLLECTIONS.LIFECYCLE_STATE, "dormant")
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(T)).and(CATALOG_COLLECTIONS.NAME.eq(ARC_DOCS)).execute();
        register();

        runToCompletion();

        assertThat(ownerOf(T, ARC_CODE)).isEqualTo("1-41");
        assertThat(ownerOf(T, ARC_DOCS)).as("a dormant row counts toward its tuple, so it moves with its repo").isEqualTo("1-41");
    }

    // ── S4: the tables the verification reads carry owner-read policies ─────────────────────────────────────

    @Test
    void theOwnerReadPoliciesOfCatalogOwnersAndGcAudit_areApplied_andTheBootCheckAsksAboutThem() throws Exception {
        register();
        assertThat(PgCatalogProbes.policyExists(ctx(), "nexus", "catalog_owners", "catalog_owners_owner_read")).isTrue();
        assertThat(PgCatalogProbes.policyExists(ctx(), "nexus", "gc_audit", "gc_audit_owner_read")).isTrue();
        assertThat(DrivingTableIsolationCheck.registeredDrivingTables(ctx()))
            .contains(new DrivingTableIsolationCheck.Table("nexus", "catalog_owners"),
                new DrivingTableIsolationCheck.Table("nexus", "gc_audit"));
    }

    @Test
    void theTableWideVerification_seesTheUnmovedRows_soItDoesNotPassOnEmptyTables() throws Exception {
        gateXr789(T);
        register();
        var verification = ctx().select(DSL.field(DSL.name("verification"), String.class)).from(table("nexus", "data_moves"))
            .where(DSL.field(DSL.name("label"), String.class).eq(LABEL)).fetchOne(DSL.field(DSL.name("verification"), String.class));
        assertThat(verification).isNotNull();
        String tableWide = verification.replace("$1", "NULL::text");
        try (Connection c = DriverManager.getConnection(pg.getJdbcUrl(), ADMIN, ADMIN_PASSWORD)) {
            assertThat(DSL.using(c, SQLDialect.POSTGRES).resultQuery(tableWide).fetch())
                .as("the migrating role, no tenant set: the unmoved repo stem is seen through the owner-read policies").isNotEmpty();
        }
        runToCompletion();
        try (Connection c = DriverManager.getConnection(pg.getJdbcUrl(), ADMIN, ADMIN_PASSWORD)) {
            assertThat(DSL.using(c, SQLDialect.POSTGRES).resultQuery(tableWide).fetch()).as("and nothing is left once moved").isEmpty();
        }
    }

    // ── S2: the destination tuple locks ─────────────────────────────────────────────────────────────────────

    /** The advisory-lock waiter: its pid and the key it waits for, or null while nobody waits. */
    private long[] waitingAdvisoryLock() {
        Field<Long> classid = DSL.field(DSL.name("classid"), Long.class);
        Field<Long> objid = DSL.field(DSL.name("objid"), Long.class);
        Field<Integer> pid = DSL.field(DSL.name("pid"), Integer.class);
        var r = ctx().select(classid, objid, pid).from(table("pg_catalog", "pg_locks"))
            .where(DSL.field(DSL.name("locktype"), String.class).eq("advisory"))
            .and(DSL.field(DSL.name("granted"), Boolean.class).isFalse()).limit(1).fetchOne();
        return r == null ? null : new long[] {(r.get(classid) << 32) | r.get(objid), r.get(pid)};
    }

    private List<Long> grantedAdvisoryKeys(int pid) {
        Field<Long> classid = DSL.field(DSL.name("classid"), Long.class);
        Field<Long> objid = DSL.field(DSL.name("objid"), Long.class);
        List<Long> keys = new ArrayList<>();
        for (var r : ctx().select(classid, objid).from(table("pg_catalog", "pg_locks"))
                .where(DSL.field(DSL.name("locktype"), String.class).eq("advisory"))
                .and(DSL.field(DSL.name("granted"), Boolean.class).isTrue())
                .and(DSL.field(DSL.name("pid"), Integer.class).eq(pid)).fetch()) {
            keys.add((r.get(classid) << 32) | r.get(objid));
        }
        return keys;
    }

    /** The six tuples (code, docs, rdr) x (1-41, 1-42) that two repo stems of the tenant move onto, in the move's models. */
    private List<CollectionTupleLock.Tuple> sixDestinationTuples() {
        List<CollectionTupleLock.Tuple> tuples = new ArrayList<>();
        for (String owner : new String[] {"1-42", "1-41"}) {                    // not in ascending order on purpose
            tuples.add(new CollectionTupleLock.Tuple(T, "rdr", owner, "voyage-context-3", "v1"));
            tuples.add(new CollectionTupleLock.Tuple(T, "docs", owner, "voyage-context-3", "v1"));
            tuples.add(new CollectionTupleLock.Tuple(T, "code", owner, "voyage-code-3", "v1"));
        }
        return tuples;
    }

    /**
     * Six destination tuples, one chunk, a Java holder of the key that is fourth in ascending order: the move's chunk waits on
     * exactly that key and, while it waits, holds exactly the three lower keys and none of the three higher ones. With three or
     * more keys, given here in no sorted order, an unordered lock sequence (ORDER BY dropped from tk) cannot pass by luck.
     * Released, the move completes.
     */
    @Test
    void theMoveTakesTheDestinationTupleLocksInAscendingOrder_andWaitsForAJavaHolderOfOne() throws Exception {
        gateXr789(T);
        collection(T, "rdr__arcaneum-2ad2825c__voyage-context-3__v1", "1-40");
        collection(T, "code__beta-deadbeef__voyage-code-3__v1", "1-40");
        collection(T, "docs__beta-deadbeef__voyage-context-3__v1", "1-40");
        collection(T, "rdr__beta-deadbeef__voyage-context-3__v1", "1-40");
        register();
        var tuples = sixDestinationTuples();
        var byKey = new TreeMap<Long, CollectionTupleLock.Tuple>();
        tuples.forEach(t -> byKey.put(CollectionTupleLock.key(ctx(), t), t));
        assertThat(byKey).as("six distinct keys").hasSize(6);
        var sorted = new ArrayList<>(byKey.keySet());
        long holderKey = sorted.get(3);
        var held = new TreeSet<>(sorted.subList(0, 3));
        var worker = worker(installed(), null, FailureHandler.BLOCK_ALWAYS, new VerifyingFinisher());
        CompletableFuture<Integer> cycle;
        try (Connection holder = DriverManager.getConnection(pg.getJdbcUrl(), ADMIN, ADMIN_PASSWORD)) {
            holder.setAutoCommit(false);
            CollectionTupleLock.lockTuple(DSL.using(holder, SQLDialect.POSTGRES), byKey.get(holderKey));
            cycle = async(worker::runCycle);
            long[] waiting = awaitWaitingAdvisoryLock(cycle);
            assertThat(waiting[0]).as("it waits for the holder's tuple key, the fourth in ascending order").isEqualTo(holderKey);
            var heldNow = new TreeSet<>(grantedAdvisoryKeys((int) waiting[1]));
            heldNow.retainAll(byKey.keySet());
            assertThat(heldNow).as("it already holds the three lower keys, and none of the higher ones").isEqualTo(held);
            assertThat(completedAt(LABEL)).isNull();
            assertThat(ownerOf(T, ARC_CODE)).as("nothing moved while the lock was held").isEqualTo("1-40");
            holder.rollback();
        }
        cycle.get(2, TimeUnit.MINUTES);

        runToCompletion();
        assertThat(ownerOf(T, ARC_CODE)).isEqualTo("1-41");
        assertThat(ownerOf(T, "code__beta-deadbeef__voyage-code-3__v1")).isEqualTo("1-42");
    }

    /** Waits for the cycle to block on an advisory lock; returns the key and the waiting backend's pid. */
    private long[] awaitWaitingAdvisoryLock(CompletableFuture<Integer> cycle) throws Exception {
        long deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(60);
        long[] waiting = null;
        while (waiting == null && System.nanoTime() < deadline) {
            assertThat(cycle.isDone()).as("the move must wait for the tuple lock, not finish").isFalse();
            waiting = waitingAdvisoryLock();
            if (waiting == null) {
                Thread.sleep(50);
            }
        }
        assertThat(waiting).as("the move's chunk is waiting on an advisory lock").isNotNull();
        return waiting;
    }

    /**
     * Twelve repo stems, chunk_size 2: a chunk locks only the destination tuples of its own rows, never the tenant's. A Java
     * holder takes the tuple of the first chunk's two rows whose key is higher, and that key is higher than every one of the other
     * ten tenant keys (the owner numbering is chosen so, below): an unscoped template would already hold all ten when it blocked.
     * Observed from a second connection in pg_locks: the blocked chunk holds exactly the other tuple of its own two rows.
     */
    @Test
    void aChunkLocksOnlyItsOwnRowsDestinationTuples_notTheTenantsTwelve() throws Exception {
        // the move mints 1-<base+1> .. 1-<base+12> in stem order; pick the base whose first two keys' maximum beats the other ten
        int base = -1;
        for (int b = 40; b < 1000 && base < 0; b++) {
            long[] k = new long[12];
            for (int i = 0; i < 12; i++) {
                k[i] = CollectionTupleLock.key(ctx(), codeTuple("1-" + (b + 1 + i)));
            }
            long chunkMax = Math.max(k[0], k[1]);
            long others = Long.MIN_VALUE;
            for (int i = 2; i < 12; i++) {
                others = Math.max(others, k[i]);
            }
            if (chunkMax > others) {
                base = b;
            }
        }
        assertThat(base).as("a base whose first chunk holds the highest key was found").isPositive();
        owner(T, "1." + base, "knowledge", "curator", null);
        var all = new TreeSet<Long>();
        for (int i = 1; i <= 12; i++) {
            collection(T, String.format("code__s%02d-%08x__voyage-code-3__v1", i, i), "1-" + base);
            all.add(CollectionTupleLock.key(ctx(), codeTuple("1-" + (base + i))));
        }
        assertThat(all).hasSize(12);
        register();
        ctx().update(table("nexus", "data_moves")).set(DSL.field(DSL.name("chunk_size"), Integer.class), 2)
            .where(DSL.field(DSL.name("label"), String.class).eq(LABEL)).execute();
        long firstA = CollectionTupleLock.key(ctx(), codeTuple("1-" + (base + 1)));
        long firstB = CollectionTupleLock.key(ctx(), codeTuple("1-" + (base + 2)));
        long holderKey = Math.max(firstA, firstB);
        long ownLower = Math.min(firstA, firstB);
        var holderTuple = firstA > firstB ? codeTuple("1-" + (base + 1)) : codeTuple("1-" + (base + 2));
        var worker = worker(installed(), null, FailureHandler.BLOCK_ALWAYS, new VerifyingFinisher());
        CompletableFuture<Integer> cycle;
        try (Connection holder = DriverManager.getConnection(pg.getJdbcUrl(), ADMIN, ADMIN_PASSWORD)) {
            holder.setAutoCommit(false);
            CollectionTupleLock.lockTuple(DSL.using(holder, SQLDialect.POSTGRES), holderTuple);
            cycle = async(worker::runCycle);
            long[] waiting = awaitWaitingAdvisoryLock(cycle);
            assertThat(waiting[0]).isEqualTo(holderKey);
            var heldNow = new TreeSet<>(grantedAdvisoryKeys((int) waiting[1]));
            heldNow.retainAll(all);
            assertThat(heldNow).as("the blocked chunk holds its own other tuple and not the tenant's ten others")
                .containsExactly(ownLower);
            holder.rollback();
        }
        cycle.get(2, TimeUnit.MINUTES);

        runToCompletion();
        assertThat(progress(LABEL).total()).as("non-vacuity: chunks of two rows").isGreaterThanOrEqualTo(6);
        for (int i = 1; i <= 12; i++) {
            assertThat(ownerOf(T, String.format("code__s%02d-%08x__voyage-code-3__v1", i, i))).isEqualTo("1-" + (base + i));
        }
    }

    private CollectionTupleLock.Tuple codeTuple(String owner) {
        return new CollectionTupleLock.Tuple(T, "code", owner, "voyage-code-3", "v1");
    }

    // ── the owner-registration lock ─────────────────────────────────────────────────────────────────────────

    @Test
    void theMoveWaitsForTheOwnerRegistrationLock_andCompletesWhenItIsReleased() throws Exception {
        gateXr789(T);
        register();
        var w = worker(installed(), null, FailureHandler.BLOCK_ALWAYS, new VerifyingFinisher());
        CompletableFuture<Integer> cycle;
        try (Connection holder = DriverManager.getConnection(pg.getJdbcUrl(), ADMIN, ADMIN_PASSWORD)) {
            holder.setAutoCommit(false);
            DSL.using(holder, SQLDialect.POSTGRES).select(DSL.function("pg_advisory_xact_lock", SQLDataType.VARCHAR,
                DSL.function("hashtext", SQLDataType.INTEGER, DSL.inline("catalog_owners/").concat(DSL.val(T))))).fetch();
            cycle = async(w::runCycle);

            long deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(60);
            boolean waiting = false;
            while (!waiting && System.nanoTime() < deadline) {
                waiting = ctx().fetchCount(table("pg_catalog", "pg_locks"),
                    DSL.field(DSL.name("locktype"), String.class).eq("advisory")
                        .and(DSL.field(DSL.name("granted"), Boolean.class).isFalse())) > 0;
                if (!waiting) {
                    Thread.sleep(50);
                }
            }
            assertThat(waiting).as("the move's chunk is waiting for the lock upsertOwner takes").isTrue();
            assertThat(cycle.isDone()).as("the cycle cannot end while the lock is held").isFalse();
            assertThat(completedAt(LABEL)).isNull();
            assertThat(ownerOf(T, ARC_CODE)).as("nothing moved while the lock was held").isEqualTo("1-40");
            holder.rollback();
        }
        cycle.get(2, TimeUnit.MINUTES);

        runToCompletion();
        assertThat(ownerOf(T, ARC_CODE)).isEqualTo("1-41");
        assertThat(ownerOf(T, ARC_DOCS)).isEqualTo("1-41");
    }

    @Test
    void theVerification_findsACuratorOwnedRowOfARepoStem_soAMoveThatDidNotMoveItIsNotCompleted() throws Exception {
        gateXr789(T);
        register();
        // a template that moves nothing: the per-tenant verification must still see the old shape
        ctx().update(table("nexus", "data_moves"))
            .set(DSL.field(DSL.name("template"), String.class),
                "UPDATE <driving_table> SET owner_id = t.owner_id WHERE <chunking_clause>")
            .where(DSL.field(DSL.name("label"), String.class).eq(LABEL)).execute();

        var w = worker(installed(), null, FailureHandler.BLOCK_ALWAYS, new VerifyingFinisher());
        w.runCycle();

        assertThat(completedAt(LABEL)).as("non-vacuity: the verification refuses an unmoved repo stem").isNull();
        assertThat(w.states().state(LABEL).orElseThrow().kind()).isEqualTo(MoveStates.Kind.BLOCKED);
        assertThat(ownerOf(T, ARC_CODE)).as("nothing moved").isEqualTo("1-40");
    }
}
