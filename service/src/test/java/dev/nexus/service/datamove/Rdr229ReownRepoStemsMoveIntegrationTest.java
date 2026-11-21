// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.datamove;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_OWNERS;
import static org.assertj.core.api.Assertions.assertThat;

import dev.nexus.service.PgCatalogProbes;
import dev.nexus.service.PgContainerHelper;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;

import java.sql.Connection;
import java.sql.DriverManager;
import java.sql.ResultSet;
import java.sql.Statement;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.TreeMap;
import java.util.TreeSet;
import java.util.List;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.TimeUnit;
import java.util.Map;
import java.util.stream.Collectors;

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
 * <p>STAND-IN FOR vectors-065. The admin role must read every tenant's {@code catalog_collections} rows (the worker's tenant
 * list, the table-wide verification), which needs the owner-read SELECT policy {@code catalog_collections_owner_read}. RDR-229
 * D2 ships it as vectors-065 and the move's own changeset carries none. Until vectors-065 is on this branch, {@link #standIn}
 * applies the datamove/owner-read-policy/policy.xml fixture (the same pattern, the same name) to catalog_collections; when
 * vectors-065 lands, delete {@code standIn} and the real policy is already there.
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
class Rdr229ReownRepoStemsMoveIntegrationTest extends DataMoveIntegrationBase {

    private static final String LABEL = "rdr229-reown-repo-stems-from-curator";
    private static final String REGISTER = "db/changelog/vectors-067-reown-repo-stems-register.xml";
    private static final String POLICY = "datamove/owner-read-policy/policy.xml";

    private static final String T = "rdr229-reown-a";
    private static final String U = "rdr229-reown-b";

    private static final String ARC_CODE = "code__arcaneum-2ad2825c__voyage-code-3__v1";
    private static final String ARC_DOCS = "docs__arcaneum-2ad2825c__voyage-context-3__v1";
    private static final String WOW_DOCS = "docs__wow-addon-dev__voyage-context-3__v1";

    private int runs;

    @BeforeEach
    void freshCatalog() throws Exception {
        ctx().deleteFrom(CATALOG_COLLECTIONS).where(CATALOG_COLLECTIONS.TENANT_ID.like("rdr229-reown-%")).execute();
        ctx().deleteFrom(CATALOG_OWNERS).where(CATALOG_OWNERS.TENANT_ID.like("rdr229-reown-%")).execute();
        ctx().deleteFrom(table("nexus", "gc_audit")).where(DSL.field(DSL.name("tenant_id"), String.class).like("rdr229-reown-%"))
            .execute();
        standIn();
    }

    // ── helpers ─────────────────────────────────────────────────────────────────────────────────────────────

    /** The stand-in for vectors-065: catalog_collections_owner_read for the migrating role, by the pattern's own changeset. */
    private void standIn() throws Exception {
        try (Connection c = DriverManager.getConnection(pg.getJdbcUrl(), ADMIN, ADMIN_PASSWORD)) {
            PgContainerHelper.runSuperuserTestChangelog(c, POLICY, "databasechangelog_test_reown_policy_" + (++runs),
                Map.of("driving_schema", "nexus", "driving_table", "catalog_collections"));
        }
        assertThat(PgCatalogProbes.policyExists(ctx(), "nexus", "catalog_collections", "catalog_collections_owner_read"))
            .as("the stand-in policy is in place (the guard did not skip it)").isTrue();
    }

    /** Registers the move by the shipped changeset, as the migrating role, exactly as a boot does. */
    private void register() throws Exception {
        // The dml_utils runs (<label>/<tenant>) outlive the data_moves row: a later test with the same label and tenant
        // would resume a run that already completed, execute no chunk, and fail verification on its fresh rows. Archive
        // them, as a tenant removal does, so every test starts with no run.
        for (String label : new String[] {LABEL, "again"}) {
            for (String run : DataMoveRuns.activeRunLabels(ctx(), label)) {
                DataMoveRuns.archive(ctx(), run);
            }
        }
        ctx().deleteFrom(table("nexus", "data_moves")).execute();
        try (Connection c = DriverManager.getConnection(pg.getJdbcUrl(), ADMIN, ADMIN_PASSWORD)) {
            PgContainerHelper.runSuperuserTestChangelog(c, REGISTER, "databasechangelog_test_reown_register_" + (++runs),
                Map.of());
        }
        assertThat(ctx().fetchCount(table("nexus", "data_moves"),
            DSL.field(DSL.name("label"), String.class).eq(LABEL))).as("registered by the shipped changeset").isEqualTo(1);
    }

    /** One worker cycle, then the move must be complete: verified, with {@code completed_at} set. */
    private void runToCompletion() {
        for (int i = 0; i < 3 && completedAt(LABEL) == null; i++) {
            worker(installed(), null, FailureHandler.BLOCK_ALWAYS, new VerifyingFinisher()).runCycle();
        }
        assertThat(completedAt(LABEL)).as("the move ran, verified and completed on the worker").isNotNull();
    }

    private void owner(String tenant, String prefix, String name, String type, String repoHash) {
        ctx().insertInto(CATALOG_OWNERS, CATALOG_OWNERS.TENANT_ID, CATALOG_OWNERS.TUMBLER_PREFIX, CATALOG_OWNERS.NAME,
                CATALOG_OWNERS.OWNER_TYPE, CATALOG_OWNERS.REPO_HASH, CATALOG_OWNERS.REPO_ROOT, CATALOG_OWNERS.NEXT_SEQ)
            .values(tenant, prefix, name, type, repoHash, "", 0L).execute();
    }

    private void collection(String tenant, String name, String owner) {
        collection(tenant, name, owner, "live", "");
    }

    private void collection(String tenant, String name, String owner, String lifecycle, String supersededBy) {
        String contentType = name.substring(0, name.indexOf("__"));
        ctx().insertInto(CATALOG_COLLECTIONS, CATALOG_COLLECTIONS.TENANT_ID, CATALOG_COLLECTIONS.NAME,
                CATALOG_COLLECTIONS.CONTENT_TYPE, CATALOG_COLLECTIONS.OWNER_ID, CATALOG_COLLECTIONS.EMBEDDING_MODEL,
                CATALOG_COLLECTIONS.MODEL_VERSION, CATALOG_COLLECTIONS.LIFECYCLE_STATE, CATALOG_COLLECTIONS.SUPERSEDED_BY)
            .values(tenant, name, contentType, owner,
                contentType.contains("code") ? "voyage-code-3" : "voyage-context-3", "v1", lifecycle, supersededBy)
            .execute();
    }

    private String ownerOf(String tenant, String name) {
        return ctx().select(CATALOG_COLLECTIONS.OWNER_ID).from(CATALOG_COLLECTIONS)
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant)).and(CATALOG_COLLECTIONS.NAME.eq(name))
            .fetchOne(CATALOG_COLLECTIONS.OWNER_ID);
    }

    /** The tenant's owners as {@code prefix=name/type/hash}, for an exact comparison. */
    private List<String> ownersOf(String tenant) {
        return ctx().select(CATALOG_OWNERS.TUMBLER_PREFIX, CATALOG_OWNERS.NAME, CATALOG_OWNERS.OWNER_TYPE,
                CATALOG_OWNERS.REPO_HASH, CATALOG_OWNERS.REPO_ROOT)
            .from(CATALOG_OWNERS).where(CATALOG_OWNERS.TENANT_ID.eq(tenant)).orderBy(CATALOG_OWNERS.TUMBLER_PREFIX)
            .fetch().stream().map(r -> r.value1() + "=" + r.value2() + "/" + r.value3() + "/"
                + (r.value4() == null ? "" : r.value4()) + "/" + r.value5()).collect(Collectors.toList());
    }

    /** The gate-xr789 shape: a curator 1.40 owns a repo's code and docs and an unrelated doc corpus; no repo owner. */
    private void gateXr789(String tenant) {
        owner(tenant, "1.40", "knowledge", "curator", null);
        collection(tenant, ARC_CODE, "1-40");
        collection(tenant, ARC_DOCS, "1-40");
        collection(tenant, WOW_DOCS, "1-40");
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

    // ── an occupied destination tuple: skipped, reported, the move completes ────────────────────────────────

    /** A native code__1-17 collection of the repo, its restored legacy twin under the curator, and one sibling of each. */
    private void restoredLegacyBesideNative(String tenant) {
        owner(tenant, "1.40", "knowledge", "curator", null);
        owner(tenant, "1.17", "arcaneum", "repo", "2ad2825c");
        collection(tenant, "code__1-17__voyage-code-3__v1", "1-17");                  // native: holds (code, 1-17, voyage-code-3, v1)
        collection(tenant, ARC_CODE, "1-40");                                          // legacy: would land on that tuple
        collection(tenant, ARC_DOCS, "1-40");                                          // legacy docs: its destination is free
        collection(tenant, "quarantine-" + ARC_CODE, "1-40", "quarantine", "");
        collection(tenant, "quarantine-" + ARC_DOCS, "1-40", "quarantine", "");
    }

    private List<String> skipReports(String tenant) {
        var op = DSL.field(DSL.name("operation"), String.class);
        return ctx().select(DSL.field(DSL.name("collection"), String.class), DSL.field(DSL.name("details"), String.class))
            .from(table("nexus", "gc_audit"))
            .where(DSL.field(DSL.name("tenant_id"), String.class).eq(tenant)).and(op.eq("rdr229_reown_skipped"))
            .orderBy(DSL.field(DSL.name("id"))).fetch().stream().map(r -> r.value1() + " " + r.value2()).toList();
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

    /** Two curator-owned stems of one repo hash and different slugs, neither with an owner yet: one free tuple, two claimants. */
    private void twoStemsRegistered(int chunkSize) throws Exception {
        owner(T, "1.40", "knowledge", "curator", null);
        collection(T, "code__a-c0ffee11__voyage-code-3__v1", "1-40");
        collection(T, "code__b-c0ffee11__voyage-code-3__v1", "1-40");
        collection(T, "code__c-deadbeef__voyage-code-3__v1", "1-40");   // another hash after the pair: pins a gap-free numbering
        collection(T, "quarantine-code__b-c0ffee11__voyage-code-3__v1", "1-40", "quarantine", "");
        register();
        ctx().update(table("nexus", "data_moves"))
            .set(DSL.field(DSL.name("chunk_size"), Integer.class), chunkSize)
            .where(DSL.field(DSL.name("label"), String.class).eq(LABEL)).execute();
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

    // ── parity with the operator's read-only dry count ──────────────────────────────────────────────────────

    /** The dry-count query bob runs on the cloud: loaded from the classpath, never copied, so the tested file is the run file. */
    private static final String COUNT_SQL_RESOURCE = "/datamove/rdr229-reown-count.sql";

    private record Pred(String action, String name, String ownerId, String dest, String reason) { }

    private static String countSql() throws Exception {
        try (var in = Rdr229ReownRepoStemsMoveIntegrationTest.class.getResourceAsStream(COUNT_SQL_RESOURCE)) {
            assertThat(in).as("the dry-count file is on the test classpath").isNotNull();
            return new String(in.readAllBytes(), java.nio.charset.StandardCharsets.UTF_8);
        }
    }

    /** The query for one tenant, in a READ ONLY transaction with SET LOCAL nexus.tenant: a write would raise 25006. */
    private List<Pred> predict(String tenant) throws Exception {
        List<Pred> out = new ArrayList<>();
        try (Connection c = DriverManager.getConnection(pg.getJdbcUrl(), ADMIN, ADMIN_PASSWORD)) {
            c.setAutoCommit(false);
            c.setReadOnly(true);
            try (Statement st = c.createStatement()) {
                st.execute("SET LOCAL nexus.tenant = '" + tenant + "'");
                st.execute("SET LOCAL statement_timeout = '60s'");
                try (ResultSet rs = st.executeQuery(countSql())) {
                    while (rs.next()) {
                        out.add(new Pred(rs.getString(1), rs.getString(2), rs.getString(3), rs.getString(4), rs.getString(5)));
                    }
                }
            } finally {
                c.rollback();
            }
        }
        return out;
    }

    private Map<String, String> ownersOfAll(String tenant) {
        Map<String, String> m = new TreeMap<>();
        ctx().select(CATALOG_COLLECTIONS.NAME, CATALOG_COLLECTIONS.OWNER_ID).from(CATALOG_COLLECTIONS)
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant)).forEach(r -> m.put(r.value1(), r.value2()));
        return m;
    }

    private Map<String, String> ownerPrefixesWithHash(String tenant) {
        Map<String, String> m = new TreeMap<>();
        ctx().select(CATALOG_OWNERS.TUMBLER_PREFIX, CATALOG_OWNERS.REPO_HASH).from(CATALOG_OWNERS)
            .where(CATALOG_OWNERS.TENANT_ID.eq(tenant)).forEach(r -> m.put(r.value1(), r.value2() == null ? "" : r.value2()));
        return m;
    }

    private Map<String, String> skipReasons(String tenant) {
        Map<String, String> m = new TreeMap<>();
        ctx().select(DSL.field(DSL.name("collection"), String.class), DSL.field("details->>'reason'", String.class))
            .from(table("nexus", "gc_audit"))
            .where(DSL.field(DSL.name("tenant_id"), String.class).eq(tenant))
            .and(DSL.field(DSL.name("operation"), String.class).eq("rdr229_reown_skipped"))
            .forEach(r -> m.put(r.value1(), r.value2()));
        return m;
    }

    /**
     * The dry count must predict exactly what the move does. Per tenant, before the move: run the query read-only (and see
     * that the data is unchanged). Then run the move to completion and compare: (a) the reown and reown_quarantine_sibling
     * names are the rows whose owner_id changed, each to the predicted owner; (b) the skip rows are the gc_audit reports,
     * name and reason; (c) the distinct {@code new:} owners are the catalog_owners rows the move inserted, and the rows moved
     * to a new owner hold its tumbler. {@code expected} is the aggregate "reown=N skip=N sibling=N new=N" (non-vacuity).
     */
    private void assertParity(String expected, String... tenants) throws Exception {
        Map<String, List<Pred>> predicted = new LinkedHashMap<>();
        Map<String, Map<String, String>> collsBefore = new LinkedHashMap<>();
        Map<String, Map<String, String>> ownersBefore = new LinkedHashMap<>();
        for (String t : tenants) {
            collsBefore.put(t, ownersOfAll(t));
            ownersBefore.put(t, ownerPrefixesWithHash(t));
            predicted.put(t, predict(t));
            assertThat(ownersOfAll(t)).as("(d) the dry count wrote nothing: collections, " + t).isEqualTo(collsBefore.get(t));
            assertThat(ownerPrefixesWithHash(t)).as("(d) the dry count wrote nothing: owners, " + t).isEqualTo(ownersBefore.get(t));
            assertThat(skipReasons(t)).as("(d) the dry count wrote nothing: gc_audit, " + t).isEmpty();
        }

        runToCompletion();

        int reown = 0, skip = 0, sibling = 0, minted = 0;
        for (String t : tenants) {
            List<Pred> preds = predicted.get(t);
            Map<String, String> before = collsBefore.get(t);
            Map<String, String> after = ownersOfAll(t);
            Map<String, String> ownersAfter = ownerPrefixesWithHash(t);

            var changed = new TreeSet<String>();
            before.forEach((name, owner) -> {
                if (!owner.equals(after.get(name))) {
                    changed.add(name);
                }
            });
            var moves = preds.stream().filter(p -> !p.action().equals("skip")).toList();
            TreeSet<String> movedNames = new TreeSet<>();
            moves.forEach(p -> movedNames.add(p.name()));
            assertThat(movedNames)
                .as("(a) reown + reown_quarantine_sibling names are the rows whose owner_id changed, " + t).isEqualTo(changed);

            var newHashes = new TreeSet<String>();
            for (Pred p : moves) {
                if (p.dest().startsWith("new:")) {
                    String hash = p.dest().substring(4);
                    newHashes.add(hash);
                    String tumbler = ownersAfter.entrySet().stream().filter(e -> e.getValue().equals(hash))
                        .map(Map.Entry::getKey).findFirst().orElse(null);
                    assertThat(tumbler).as("(c) the move minted the owner for hash " + hash + ", " + t).isNotNull();
                    assertThat(after.get(p.name())).as("(c) " + p.name() + " moved to the minted owner's tumbler")
                        .isEqualTo(tumbler.replace('.', '-'));
                } else {
                    assertThat(after.get(p.name())).as("(a) " + p.name() + " moved to the predicted owner").isEqualTo(p.dest());
                }
            }
            var inserted = new TreeSet<>(ownersAfter.keySet());
            inserted.removeAll(ownersBefore.get(t).keySet());
            assertThat(inserted).as("(c) the number of distinct new: owners is the number of owners the move inserted, " + t)
                .hasSize(newHashes.size());
            TreeSet<String> insertedHashes = new TreeSet<>();
            inserted.forEach(prefix -> insertedHashes.add(ownersAfter.get(prefix)));
            assertThat(insertedHashes).as("(c) the inserted owners carry the predicted hashes, " + t).isEqualTo(newHashes);

            Map<String, String> predictedSkips = new TreeMap<>();
            preds.stream().filter(p -> p.action().equals("skip")).forEach(p -> predictedSkips.put(p.name(), p.reason()));
            assertThat(skipReasons(t)).as("(b) skip names and reasons are the gc_audit reports, " + t).isEqualTo(predictedSkips);

            reown += (int) moves.stream().filter(p -> p.action().equals("reown")).count();
            sibling += (int) moves.stream().filter(p -> p.action().equals("reown_quarantine_sibling")).count();
            skip += predictedSkips.size();
            minted += newHashes.size();
        }
        assertThat("reown=" + reown + " skip=" + skip + " sibling=" + sibling + " new=" + minted)
            .as("non-vacuity: the shape's prediction").isEqualTo(expected);
    }

    @Test
    void dryCountParity_gateXr789Shape() throws Exception {
        gateXr789(T);
        collection(T, "knowledge__distributed-systems__voyage-context-3__v1", "1-40");
        collection(T, "quarantine-code__arcaneum-2ad2825c__voyage-code-3__v1", "1-40", "quarantine", "");
        collection(T, "code__arcaneum-2ad2825c__voyage-code-3__v0", "1-40", "live", "code__arcaneum-2ad2825c__voyage-code-3__v1");
        register();
        assertParity("reown=2 skip=0 sibling=1 new=1", T);
    }

    @Test
    void dryCountParity_restoredLegacyBesideItsNativeTwin() throws Exception {
        restoredLegacyBesideNative(T);
        register();
        assertParity("reown=1 skip=1 sibling=1 new=0", T);
    }

    @Test
    void dryCountParity_twoStemsOfOneHash_oneChunk() throws Exception {
        twoStemsRegistered(1000);
        assertParity("reown=2 skip=1 sibling=0 new=2", T);
    }

    @Test
    void dryCountParity_twoStemsOfOneHash_chunkSizeOne() throws Exception {
        twoStemsRegistered(1);
        assertParity("reown=2 skip=1 sibling=0 new=2", T);
    }

    @Test
    void dryCountParity_aDocsOnlyStemWhoseHashHasARepoOwner() throws Exception {
        owner(T, "1.40", "knowledge", "curator", null);
        owner(T, "1.7", "arcaneum-checkout", "repo", "2ad2825c");
        owner(T, "1.9", "some-repo", "repo", "11112222");
        collection(T, ARC_DOCS, "1-40");
        collection(T, "rdr__arcaneum-2ad2825c__voyage-context-3__v1", "1-9");
        collection(T, "docs__notes-0badc0de__voyage-context-3__v1", "1-40");
        register();
        assertParity("reown=1 skip=0 sibling=0 new=0", T);
    }

    @Test
    void dryCountParity_aStemWhoseRowsFallInDifferentChunks() throws Exception {
        gateXr789(T);
        collection(T, "rdr__arcaneum-2ad2825c__voyage-context-3__v1", "1-40");
        register();
        ctx().update(table("nexus", "data_moves"))
            .set(DSL.field(DSL.name("chunk_size"), Integer.class), 1)
            .where(DSL.field(DSL.name("label"), String.class).eq(LABEL)).execute();
        assertParity("reown=3 skip=0 sibling=0 new=1", T);
    }

    @Test
    void dryCountParity_aFreshInstallWithNoRows() throws Exception {
        register();
        assertParity("reown=0 skip=0 sibling=0 new=0", T);
    }

    @Test
    void dryCountParity_aTenantPair() throws Exception {
        gateXr789(T);
        owner(U, "1.3", "knowledge", "curator", null);
        owner(U, "1.4", "older-repo", "repo", "cafe0001");
        collection(U, ARC_CODE, "1-3");
        collection(U, ARC_DOCS, "1-3");
        register();
        assertParity("reown=4 skip=0 sibling=0 new=2", T, U);
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

    // ── the owner-registration lock ─────────────────────────────────────────────────────────────────────────

    @Test
    void theMoveWaitsForTheOwnerRegistrationLock_andCompletesWhenItIsReleased() throws Exception {
        gateXr789(T);
        register();
        var w = worker(installed(), null, FailureHandler.BLOCK_ALWAYS, new VerifyingFinisher());
        CompletableFuture<Integer> cycle;
        try (Connection holder = DriverManager.getConnection(pg.getJdbcUrl(), ADMIN, ADMIN_PASSWORD)) {
            holder.setAutoCommit(false);
            try (var st = holder.prepareStatement("SELECT pg_advisory_xact_lock(hashtext('catalog_owners/' || CAST(? AS text)))")) {
                st.setString(1, T);
                st.execute();
            }
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
