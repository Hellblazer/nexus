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
import java.util.List;
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
 * <p>Falsifiers, by hand: drop the {@code CROSS JOIN lck} and the lock is never taken (no test sees it, which is why the
 * template carries it by construction); make {@code repo_owner} read only {@code ins} and the existing-owner test moves
 * nothing; drop {@code OR EXISTS (...)} from {@code cand} and the chunk-straddle test leaves docs rows on the curator.
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
        assertThat(ownerOf(T, "quarantine-code__arcaneum-2ad2825c__voyage-code-3__v1")).as("quarantine is not touched")
            .isEqualTo("1-40");
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
        DataMove first = DataMoveRegistry.reload(ctx(), LABEL).orElseThrow();
        DataMoveTestFixtures.registerOn(ctx(), "again", "nexus", "catalog_collections", first.chunkSize(), 0, null,
            "stamp-template.sql");
        ctx().update(table("nexus", "data_moves"))
            .set(DSL.field(DSL.name("template"), String.class), first.template())
            .set(DSL.field(DSL.name("verification"), String.class), first.verification())
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
