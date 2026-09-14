// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.db.Chash;
import dev.nexus.service.db.TaxonomyRepository;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.jooq.binding.Vector;
import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Optional;

import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.TOPICS;
import static dev.nexus.service.jooq.nexus.Tables.TOPIC_ASSIGNMENTS;
import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * nexus-4a8pn — write-path census follow-through for {@code topics.doc_count}.
 *
 * <p>The census (bead notes, {@code bd show nexus-4a8pn}) found three
 * {@link TaxonomyRepository} batch-persist methods — {@code persistSplit},
 * {@code persistRebuildTopics}, {@code persistDiscoveredTopics} — SEEDING
 * {@code TOPICS.DOC_COUNT} straight from the caller-supplied spec's {@code
 * doc_count} field, in contradiction of RDR-154 P0's own doctrine (quoted in
 * {@code persistSplit}): "the trigger is the sole writer" of {@code doc_count}.
 * {@code batchInsertAssignments} returns early on an empty {@code doc_ids} list,
 * so a spec claiming a nonzero {@code doc_count} with nothing insertable never
 * fires the AFTER INSERT trigger and the seeded value goes uncorrected — a
 * reachable over-count through the live {@code POST /v1/taxonomy/topics/
 * persist_split}, {@code /persist_rebuild}, and {@code /persist_discovered}
 * routes. The fix (same commit) seeds 0 in all three call sites and leaves the
 * trigger to set the true value.
 *
 * <p>Cases (a)-(d) below each assert every affected topic's {@code doc_count}
 * equals an independent {@code COUNT(*)} of its own {@code topic_assignments}
 * rows, in every write-path shape the census found reachable. Case (e) is a
 * SEPARATE, previously-untested assumption the trigger's whole design depends
 * on: that {@code topic_assignments_chunk_fk}'s {@code ON DELETE CASCADE} from
 * {@code nexus.chunks} (taxonomy-012-doc-id-chunk-fk.xml) still fires the AFTER
 * DELETE STATEMENT trigger's {@code old_rows} transition table for the
 * cascade-deleted rows, not only for a direct {@code DELETE} against {@code
 * topic_assignments} itself — measured here, not merely asserted.
 *
 * <p>Case (f) is a SECOND, independent hypothesis (critic T2
 * {@code nexus/4a8pn-critic-2026-09-14}): the cloud drift signature this bead
 * was opened against (over-count only, large magnitudes, e.g. actual=747
 * doc_count=964) cannot be explained by (a)-(d) alone, since the trigger's
 * recompute is a full {@code COUNT(*)}, not a delta — the instant ANY row
 * lands for a topic created through the fixed paths, doc_count is forced
 * correct. The surviving hypothesis: {@code taxonomy-010-source-collection-
 * backfill.xml} (:273, :327) and {@code taxonomy-012-doc-id-chunk-fk.xml}
 * (:199) toggle {@code NO FORCE ROW LEVEL SECURITY} on {@code
 * topic_assignments}/{@code chunks} for their remediation DELETEs but never
 * touch {@code nexus.topics}' own FORCE RLS, running as {@code nexus_admin}
 * (owner, NOT BYPASSRLS) with no {@code nexus.tenant} GUC set — so the AFTER
 * DELETE trigger's (SECURITY INVOKER) {@code UPDATE nexus.topics} would hit
 * {@code nexus.topics}' {@code tenant_isolation} policy (
 * {@code USING (tenant_id = current_setting('nexus.tenant', true))},
 * taxonomy-001-baseline.xml:170-172) with the GUC unset, matching ZERO rows,
 * leaving {@code doc_count} stuck at its pre-delete value while the real
 * count drops. Case (f) reproduces that exact shape in-container; MEASURED
 * 2026-09-14, CONFIRMED. Per the no-silent-fallbacks doctrine, the fix
 * (taxonomy-017-doc-count-posture-tripwire.xml) makes both trigger functions
 * RAISE instead of silently under-counting when the calling session's GUC
 * does not cover the transition table's tenant_id(s) — case (f) now asserts
 * that RAISE and that nothing was left half-changed; case (g) is the
 * counterpart proving a correct-posture cascade (topic delete cascading its
 * assignments) does NOT raise, since the guard is on posture, never on the
 * UPDATE's own (possibly legitimately zero) row count. Case (h), added in the
 * SAME 2026-09-14 fix round (the guard's first version broke 14 existing test
 * fixture classes that seed topic_assignments as the container superuser with
 * no GUC), proves a superuser/BYPASSRLS role is EXEMPT from the guard — FORCE
 * ROW LEVEL SECURITY never applies to either, so the guard would protect
 * nothing by firing there, and it must recount correctly instead of raising.
 *
 * <p>Every fixture in this class goes through generated jOOQ DSL / {@link
 * PgContainerHelper} — no raw SQL strings (Sam's directive). {@link
 * PgContainerHelper#insertChunk1024} seeds {@code nexus.chunks} rows (required
 * by {@code topic_assignments_chunk_fk} before any {@code doc_id} referencing
 * them can be assigned); {@link PgContainerHelper#insertCollection} registers
 * the owning {@code catalog_collections} row {@code ensureCollectionRegistered}
 * requires.
 *
 * <p>DEDICATED container (not shared): case (f) needs {@link
 * PgContainerHelper#bootstrapAdminRole} to reassign ownership of {@code
 * nexus.topic_assignments} — global schema state that must not leak into any
 * other test class's shared-cluster assumptions ({@code
 * PgContainerHelper#start()}'s own javadoc names exactly this class of
 * mutation as disqualifying; same precedent as {@code
 * Taxonomy010BackfillDirectIntegrationTest}).
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class TopicsDocCountWritePathsTest {

    private static final String SVC_ROLE   = "svc_wpath_test";
    private static final String SVC_PASS   = "svc_wpath_test_pass";
    private static final String ADMIN_ROLE = "admin_wpath_test";
    private static final String ADMIN_PASS = "admin_wpath_test_pass";
    private static final String TENANT     = "wpath-tenant";
    private static final int    DIM        = 1024;

    private static final float[] FIXED_VEC = fixedVec();

    private static float[] fixedVec() {
        float[] v = new float[DIM];
        v[0] = 1.0f;
        return v;
    }

    PostgreSQLContainer<?> pg;
    TenantScope tenantScope;
    HikariDataSource svcDs;
    HikariDataSource adminDs;
    TaxonomyRepository repo;

    @BeforeAll
    void startAll() throws Exception {
        // startDedicated(), not start(): case (f) reassigns nexus.topic_assignments'
        // ownership -- see the class javadoc's "DEDICATED container" note.
        pg = PgContainerHelper.startDedicated();

        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.bootstrapServiceRole(su, SVC_ROLE, SVC_PASS);
        }
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.bootstrapAdminRole(su, ADMIN_ROLE, ADMIN_PASS);
        }

        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(SVC_ROLE);
        cfg.setPassword(SVC_PASS);
        cfg.setMaximumPoolSize(8);
        cfg.setAutoCommit(true);
        svcDs = new HikariDataSource(cfg);
        tenantScope = new TenantScope(svcDs);
        repo = new TaxonomyRepository(tenantScope);

        var adminCfg = new HikariConfig();
        adminCfg.setJdbcUrl(pg.getJdbcUrl());
        adminCfg.setUsername(ADMIN_ROLE);
        adminCfg.setPassword(ADMIN_PASS);
        adminCfg.setMaximumPoolSize(2);
        adminCfg.setAutoCommit(true);
        adminDs = new HikariDataSource(adminCfg);
    }

    @AfterAll
    void stopAll() {
        if (svcDs   != null) svcDs.close();
        if (adminDs != null) adminDs.close();
        if (pg      != null) pg.stop();
    }

    // ── fixtures ────────────────────────────────────────────────────────────────

    private void seedCollection(String collection) {
        tenantScope.withTenant(TENANT, ctx -> {
            PgContainerHelper.insertCollection(ctx, TENANT, collection);
            return null;
        });
    }

    /** Seeds a {@code nexus.chunks} row (required by {@code
     *  topic_assignments_chunk_fk}) and returns its 64-hex chash — usable
     *  directly as a {@code doc_id} in a spec's {@code doc_ids} list. */
    private String seedChunk(String collection, String seed) {
        String hex = Chash.ofText(collection + "#" + seed).toHex();
        byte[] chashBytes = Chash.fromHex(hex).toBytes();
        tenantScope.withTenant(TENANT, ctx -> {
            PgContainerHelper.insertChunk1024(ctx, TENANT, collection, chashBytes, Vector.of(FIXED_VEC));
            return null;
        });
        return hex;
    }

    private int docCountOf(long topicId) {
        Integer v = tenantScope.withTenant(TENANT, ctx ->
            ctx.select(TOPICS.DOC_COUNT).from(TOPICS)
               .where(TOPICS.TENANT_ID.eq(TENANT), TOPICS.ID.eq(topicId))
               .fetchOne(TOPICS.DOC_COUNT));
        assertThat(v).as("topic %d must exist", topicId).isNotNull();
        return v;
    }

    private int actualAssignmentCount(long topicId) {
        return tenantScope.withTenant(TENANT, ctx ->
            ctx.fetchCount(TOPIC_ASSIGNMENTS,
                TOPIC_ASSIGNMENTS.TENANT_ID.eq(TENANT).and(TOPIC_ASSIGNMENTS.TOPIC_ID.eq(topicId))));
    }

    /** Inserts one {@code topic_assignments} row directly via jOOQ DSL — used to
     *  seed a pre-existing assignment organically (through the real AFTER INSERT
     *  trigger), outside of any {@code persist*} call under test. */
    private void insertAssignmentDirect(String docIdHex, long topicId, String collection) {
        tenantScope.withTenant(TENANT, ctx -> {
            ctx.insertInto(TOPIC_ASSIGNMENTS,
                    TOPIC_ASSIGNMENTS.TENANT_ID, TOPIC_ASSIGNMENTS.DOC_ID,
                    TOPIC_ASSIGNMENTS.TOPIC_ID, TOPIC_ASSIGNMENTS.ASSIGNED_BY,
                    TOPIC_ASSIGNMENTS.SOURCE_COLLECTION)
               .values(TENANT, Chash.fromHex(docIdHex).toBytes(), topicId, "manual", collection)
               .execute();
            return null;
        });
    }

    private void assertDocCountExact(long topicId) {
        int reported = docCountOf(topicId);
        int actual   = actualAssignmentCount(topicId);
        assertThat(reported)
            .as("topic %d: doc_count must equal an independent COUNT(*) of its"
                + " topic_assignments rows", topicId)
            .isEqualTo(actual);
    }

    /** A spec map carrying every key any of the three write paths under test
     *  reads (per-path irrelevant keys are simply ignored by the reader that
     *  doesn't use them) — including {@code doc_count}, deliberately set to a
     *  value that DIFFERS from the real assignment count in every case, so a
     *  regression that starts trusting it again shows up immediately via
     *  {@link #assertDocCountExact}. */
    private static Map<String, Object> spec(String label, int claimedDocCount, List<String> docIds) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("label", label);
        m.put("doc_count", claimedDocCount);
        m.put("created_at", "2026-01-01T00:00:00Z");
        m.put("terms", "t");
        m.put("terms_json", "t");
        m.put("review_status", "pending");
        m.put("assigned_by", "hdbscan");
        m.put("doc_ids", docIds);
        return m;
    }

    // ── (a) persistDiscoveredTopics: claimed doc_count, zero insertable doc_ids ──

    @Test
    void persistDiscoveredTopics_emptyDocIdsLeavesDocCountZero() {
        String collection = "knowledge__wpath_disc_empty__voyage-code-3__v1";
        seedCollection(collection);

        List<Long> ids = repo.persistDiscoveredTopics(TENANT, collection,
            List.of(spec("disc-empty", 3, List.of())));

        assertThat(ids).hasSize(1);
        assertThat(actualAssignmentCount(ids.get(0))).isEqualTo(0);
        assertDocCountExact(ids.get(0));
    }

    // ── (b) persistDiscoveredTopics: claimed doc_count, duplicate doc_ids collapse ──

    @Test
    void persistDiscoveredTopics_duplicateDocIdsCollapseToDistinctCount() {
        String collection = "knowledge__wpath_disc_dup__voyage-code-3__v1";
        seedCollection(collection);
        String a = seedChunk(collection, "a");
        String b = seedChunk(collection, "b");

        List<Long> ids = repo.persistDiscoveredTopics(TENANT, collection,
            List.of(spec("disc-dup", 5, List.of(a, a, b))));

        assertThat(ids).hasSize(1);
        assertThat(actualAssignmentCount(ids.get(0))).isEqualTo(2);
        assertDocCountExact(ids.get(0));
    }

    // ── (c) persistRebuildTopics: both shapes plus a manual transfer ───────────

    @Test
    void persistRebuildTopics_emptyAndDuplicateAndManualTransfer_exact() {
        String collection = "knowledge__wpath_rebuild__voyage-code-3__v1";
        seedCollection(collection);
        String a = seedChunk(collection, "a");
        String b = seedChunk(collection, "b");
        String c = seedChunk(collection, "c");

        List<Map<String, Object>> specs = List.of(
            spec("rebuild-empty", 4, List.of()),
            spec("rebuild-dup", 7, List.of(a, a, b)));
        Map<String, Object> manualTransfers = Map.of(c, 1);

        List<Long> ids = repo.persistRebuildTopics(TENANT, collection, specs, manualTransfers);

        assertThat(ids).hasSize(2);
        assertThat(actualAssignmentCount(ids.get(0))).isEqualTo(0);
        assertDocCountExact(ids.get(0));
        assertThat(actualAssignmentCount(ids.get(1))).isEqualTo(3);
        assertDocCountExact(ids.get(1));
    }

    // ── (d) persistSplit: parent zeroed, one empty child, one real child ───────

    @Test
    void persistSplit_emptyChildLeavesZero_realChildExact_parentZeroed() {
        String collection = "knowledge__wpath_split__voyage-code-3__v1";
        seedCollection(collection);
        String parentDoc = seedChunk(collection, "parent");
        String x = seedChunk(collection, "x");
        String y = seedChunk(collection, "y");

        long parentId = repo.insertTopic(TENANT, "split-parent", null, collection, 0,
            "2026-01-01T00:00:00Z", null);
        // One real pre-existing assignment on the parent, via a genuine direct
        // INSERT (fires the AFTER INSERT trigger organically) -- not through the
        // method under test.
        insertAssignmentDirect(parentDoc, parentId, collection);
        assertThat(docCountOf(parentId)).isEqualTo(1);

        List<Map<String, Object>> childSpecs = List.of(
            spec("split-child-empty", 4, List.of()),
            spec("split-child-real", 9, List.of(x, y)));

        List<Long> childIds = repo.persistSplit(TENANT, parentId, collection, childSpecs);

        assertThat(childIds).hasSize(2);
        // Parent zeroing is pre-existing, correct behavior (the parent's
        // assignments are DELETEd inside persistSplit, firing the AFTER DELETE
        // trigger) -- asserted here as the RED test's own precondition, not the
        // bug under test.
        assertThat(actualAssignmentCount(parentId)).isEqualTo(0);
        assertDocCountExact(parentId);

        assertThat(actualAssignmentCount(childIds.get(0))).isEqualTo(0);
        assertDocCountExact(childIds.get(0));
        assertThat(actualAssignmentCount(childIds.get(1))).isEqualTo(2);
        assertDocCountExact(childIds.get(1));
    }

    // ── (e) chunk cascade: does ON DELETE CASCADE still fire the trigger? ──────

    @Test
    void chunkDeleteCascade_stillFiresDocCountTrigger() {
        String collection = "knowledge__wpath_cascade__voyage-code-3__v1";
        seedCollection(collection);
        String doc = seedChunk(collection, "cascade");

        long topicId = repo.insertTopic(TENANT, "cascade-topic", null, collection, 0,
            "2026-01-01T00:00:00Z", null);
        insertAssignmentDirect(doc, topicId, collection);
        assertThat(docCountOf(topicId)).isEqualTo(1);

        // Delete the nexus.chunks row directly via jOOQ DSL. No clean public
        // CatalogRepository entry point for a single-chash delete was reachable
        // without pulling in its staging/drop-tracking machinery (sweepChunks is
        // private and shaped around a whole-collection sweep) -- this is the
        // fallback the relay authorized: jOOQ DSL, never a raw SQL string.
        tenantScope.withTenant(TENANT, ctx -> {
            int deleted = ctx.deleteFrom(CHUNKS)
                .where(CHUNKS.TENANT_ID.eq(TENANT),
                       CHUNKS.COLLECTION.eq(collection),
                       CHUNKS.CHASH.eq(Chash.fromHex(doc).toBytes()))
                .execute();
            assertThat(deleted).as("the seeded chunk row must exist to delete").isEqualTo(1);
            return null;
        });

        assertThat(actualAssignmentCount(topicId))
            .as("topic_assignments_chunk_fk's ON DELETE CASCADE must remove the"
                + " assignment referencing the deleted chunk")
            .isEqualTo(0);
        assertThat(docCountOf(topicId))
            .as("the cascaded chunk DELETE must still fire"
                + " trg_topic_assignments_doc_count_del's AFTER DELETE STATEMENT"
                + " trigger (old_rows transition table capturing the cascaded row)"
                + " -- not only a direct DELETE issued against topic_assignments"
                + " itself")
            .isEqualTo(0);
    }

    // ── (f) admin-role DELETE, topics left FORCE RLS'd, no GUC: must RAISE ─────

    /**
     * Reproduces taxonomy-010/012's EXACT remediation shape: {@code
     * ALTER TABLE nexus.topic_assignments NO FORCE ROW LEVEL SECURITY}, then a
     * {@code DELETE} on {@code topic_assignments}, as a role that (a) OWNS {@code
     * topic_assignments} (so the {@code ALTER TABLE} toggle is even permitted —
     * see {@link PgContainerHelper#bootstrapAdminRole}), (b) is NOSUPERUSER /
     * NOBYPASSRLS, and (c) never sets {@code nexus.tenant} on this connection
     * (mirroring Liquibase's migration connection, which never runs {@link
     * TenantScope#withTenant}). {@code nexus.topics} is deliberately left FORCE
     * RLS'd, unchanged — neither migration touches it.
     *
     * <p>The {@code [NO] FORCE ROW LEVEL SECURITY} toggle goes through {@link
     * PgContainerHelper#setForceRls} (a {@code nexus_test.set_force_rls} SQL-
     * function call rendered via {@code ctx.render}, never a hand-typed SQL
     * string in this file) — jOOQ has no typed DSL form for this Postgres-only
     * RLS DDL extension, the same gap that helper's own javadoc documents.
     *
     * <p><b>MEASURED (2026-09-14) PRE-taxonomy-017: CONFIRMED.</b> Before the
     * taxonomy-017-doc-count-posture-tripwire.xml changeset landed, this exact
     * repro left {@code doc_count} stuck at 2 (the pre-delete value) while the
     * real assignment count dropped to 0 — the cloud's confirmed over-count
     * signature. A SECURITY DEFINER-based fix was investigated and rejected as
     * unsound (no changeset-created DEFINER precedent exists in this changelog;
     * {@code nexus_admin} — the real migration role — is created {@code
     * NOSUPERUSER NOCREATEDB NOCREATEROLE} with no {@code BYPASSRLS},
     * {@code pg_provision.py}:1557-1559 / :525, so a DEFINER function it owns
     * would stay exactly as subject to {@code topics}' FORCE RLS as today's
     * INVOKER body; the one way it COULD work — toggling FORCE inside the
     * trigger on every firing — takes {@code ACCESS EXCLUSIVE} and would
     * serialize every concurrent writer, worse than the deadlock taxonomy-013/
     * 015 exist to close). Per the no-silent-fallbacks doctrine and Sam's
     * directive to fix the bypass at the write: taxonomy-017 makes the trigger
     * RAISE instead — see that changeset's own header for the full derivation.
     *
     * <p>Now, POST-taxonomy-017: the SAME repro must RAISE (naming the tenant
     * and the remedy) rather than silently under-count, and must leave BOTH the
     * assignment rows and {@code doc_count} completely untouched — under
     * {@code adminConn}'s {@code autocommit=true}, the raising DELETE is its
     * own implicit transaction, so nothing from it lands.
     */
    @Test
    void adminRoleDelete_topicsForceRlsNoGuc_postureTripwireRaises_stateUnchanged() throws Exception {
        String collection = "knowledge__wpath_rlsdel__voyage-code-3__v1";
        seedCollection(collection);
        String a = seedChunk(collection, "rls-a");
        String b = seedChunk(collection, "rls-b");

        long topicId = repo.insertTopic(TENANT, "rls-del-topic", null, collection, 0,
            "2026-01-01T00:00:00Z", null);
        insertAssignmentDirect(a, topicId, collection);
        insertAssignmentDirect(b, topicId, collection);
        assertThat(actualAssignmentCount(topicId)).isEqualTo(2);
        assertThat(docCountOf(topicId)).isEqualTo(2);

        try (Connection adminConn = adminDs.getConnection()) {
            adminConn.setAutoCommit(true);
            PgContainerHelper.setForceRls(adminConn, TOPIC_ASSIGNMENTS, false);
            try {
                DSLContext adminCtx = DSL.using(adminConn, SQLDialect.POSTGRES);
                assertThatThrownBy(() ->
                    adminCtx.deleteFrom(TOPIC_ASSIGNMENTS)
                        .where(TOPIC_ASSIGNMENTS.TENANT_ID.eq(TENANT),
                               TOPIC_ASSIGNMENTS.TOPIC_ID.eq(topicId))
                        .execute())
                    .as("nexus-4a8pn taxonomy-017 posture tripwire: the trigger must RAISE,"
                        + " not silently under-count, when the deleting connection's"
                        + " nexus.tenant GUC does not cover the tenant_id being deleted")
                    .hasMessageContaining("topics_doc_count_recount_del")
                    .hasMessageContaining("nexus.tenant")
                    .hasMessageContaining("unset")
                    .hasMessageContaining(TENANT);
            } finally {
                PgContainerHelper.setForceRls(adminConn, TOPIC_ASSIGNMENTS, true);
            }
        }

        assertThat(actualAssignmentCount(topicId))
            .as("the raised exception must have left both assignment rows in place")
            .isEqualTo(2);
        assertThat(docCountOf(topicId))
            .as("doc_count must be untouched -- the trigger raised before its own UPDATE"
                + " ever ran")
            .isEqualTo(2);
    }

    // ── (g) topics DELETE, correct posture: cascade must NOT raise ─────────────

    /**
     * The posture-tripwire counterpart to case (f): {@link
     * TaxonomyRepository#deleteTopic} always runs through {@link
     * TenantScope#withTenant}, so {@code nexus.tenant} is correctly set to
     * {@code TENANT} for the whole call — the CORRECT posture. Deleting a topic
     * cascades (FK {@code ON DELETE CASCADE}) to its {@code topic_assignments}
     * rows, firing {@code trg_topic_assignments_doc_count_del} with {@code
     * old_rows} carrying exactly {@code TENANT} — the guard must NOT raise. The
     * trigger's own {@code UPDATE nexus.topics WHERE id = <deleted topic>} then
     * legitimately matches ZERO rows (the topics row itself is already gone,
     * deleted earlier in the SAME outer statement, before the FK cascade fires)
     * — proving the guard is on POSTURE, never on the UPDATE's own row count,
     * exactly as taxonomy-017's header states. An unrelated sibling topic in the
     * same collection must be completely unaffected.
     */
    @Test
    void deleteTopic_correctPosture_cascadesWithoutRaising_otherTopicExact() {
        String collection = "knowledge__wpath_delcorrect__voyage-code-3__v1";
        seedCollection(collection);
        String a = seedChunk(collection, "delcorrect-a");
        String b = seedChunk(collection, "delcorrect-b");
        String keep = seedChunk(collection, "delcorrect-keep");

        long topicToDelete = repo.insertTopic(TENANT, "del-correct-victim", null, collection, 0,
            "2026-01-01T00:00:00Z", null);
        insertAssignmentDirect(a, topicToDelete, collection);
        insertAssignmentDirect(b, topicToDelete, collection);
        assertThat(docCountOf(topicToDelete)).isEqualTo(2);

        long topicToKeep = repo.insertTopic(TENANT, "del-correct-survivor", null, collection, 0,
            "2026-01-01T00:00:00Z", null);
        insertAssignmentDirect(keep, topicToKeep, collection);
        assertThat(docCountOf(topicToKeep)).isEqualTo(1);

        Optional<String> deletedCollection = repo.deleteTopic(TENANT, topicToDelete);
        assertThat(deletedCollection).contains(collection);

        assertThat(actualAssignmentCount(topicToDelete)).isEqualTo(0);
        assertThat(actualAssignmentCount(topicToKeep)).isEqualTo(1);
        assertDocCountExact(topicToKeep);
    }

    // ── (h) superuser DELETE, no GUC: RLS-exempt role recounts correctly ───────

    /**
     * The 2026-09-14 fix-round regression pin: the SAME shape as case (f) — a
     * DELETE on {@code topic_assignments} with no {@code nexus.tenant} GUC set
     * — but run as the container SUPERUSER ({@link #pg}{@code .createConnection}),
     * which is unconditionally exempt from RLS (FORCE ROW LEVEL SECURITY never
     * applies to a superuser, regardless of the GUC — no {@code [NO] FORCE}
     * toggle is needed or used here). The posture tripwire's role check
     * ({@code NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = current_user
     * AND (rolsuper OR rolbypassrls))}) must recognize this and NOT raise: this
     * trigger's own {@code UPDATE nexus.topics} cannot be silently filtered for
     * a role RLS never applies to, so there is nothing for the guard to protect
     * against here — it must recount correctly instead. This is exactly the
     * shape the 14 existing test-fixture classes named in taxonomy-017's own
     * header (which seed {@code topic_assignments} directly as the superuser,
     * no GUC) depend on; the guard's first version broke all of them.
     */
    @Test
    void superuserDelete_noGuc_recountsCorrectly_doesNotRaise() throws Exception {
        String collection = "knowledge__wpath_sudel__voyage-code-3__v1";
        seedCollection(collection);
        String a = seedChunk(collection, "su-a");
        String b = seedChunk(collection, "su-b");

        long topicId = repo.insertTopic(TENANT, "su-del-topic", null, collection, 0,
            "2026-01-01T00:00:00Z", null);
        insertAssignmentDirect(a, topicId, collection);
        insertAssignmentDirect(b, topicId, collection);
        assertThat(actualAssignmentCount(topicId)).isEqualTo(2);
        assertThat(docCountOf(topicId)).isEqualTo(2);

        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext suCtx = DSL.using(su, SQLDialect.POSTGRES);
            int deleted = suCtx.deleteFrom(TOPIC_ASSIGNMENTS)
                .where(TOPIC_ASSIGNMENTS.TENANT_ID.eq(TENANT),
                       TOPIC_ASSIGNMENTS.TOPIC_ID.eq(topicId))
                .execute();
            assertThat(deleted).as("both seeded assignment rows must be deleted").isEqualTo(2);
        }

        assertThat(actualAssignmentCount(topicId)).isEqualTo(0);
        assertThat(docCountOf(topicId))
            .as("a superuser (RLS-exempt) DELETE with no GUC set must still recount"
                + " correctly -- the posture tripwire's role check must not fire for a"
                + " role FORCE RLS never applies to")
            .isEqualTo(0);
    }
}
