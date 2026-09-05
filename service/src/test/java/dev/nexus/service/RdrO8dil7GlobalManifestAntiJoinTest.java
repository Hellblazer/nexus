// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.db.CatalogRepository;
import dev.nexus.service.db.Chash;
import dev.nexus.service.db.StagingPromoteOps;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.vectors.DimTables;
import dev.nexus.service.vectors.Embedder;
import dev.nexus.service.vectors.PgVectorRepository;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.MethodOrderer;
import org.junit.jupiter.api.Order;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.junit.jupiter.api.TestMethodOrder;
import org.testcontainers.containers.PostgreSQLContainer;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.sql.Connection;
import java.util.List;
import java.util.Map;
import java.util.stream.Collectors;
import java.util.stream.IntStream;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-191 GATE-2 (nexus-o8dil.7) acceptance item 3 — THE REGRESSION TRIPWIRE.
 *
 * <p><strong>REBUILD 2026-08-13</strong> (Hal-authorized). The commit
 * d9e21c73 predecessor of this class was deliberately dropped; this is a
 * from-scratch rebuild re-aligned to THREE contract changes that landed
 * after that predecessor was authored — see T2 {@code
 * nexus/rdr-191-gate2-tripwire-rebuild-2026-08-13} for the full design-delta
 * record. In short:
 * <ol>
 *   <li><strong>catalog-025</strong> made {@code catalog_document_chunks
 *       .collection} {@code NOT NULL} with no sentinel, no DEFAULT. The
 *       NULL-collection arm of GATE-2's original criterion is now enforced
 *       by the SCHEMA ITSELF — a raw INSERT/UPDATE that tries to write a
 *       NULL there fails at the database, not at some application layer
 *       this suite could observe going wrong. There is nothing left for an
 *       application-level test to prove on that axis, so it is DROPPED
 *       here, not ported. (nexus-c30ew, the GC-chain NULL-collection
 *       laundering bug the predecessor detected via three SKIPped tests,
 *       is CLOSED as an ARTIFACT of the now-rejected collection-inference
 *       design — see that bead's close note. The six-arm anti-join
 *       rewrite it called for is CANCELLED, not deferred.)</li>
 *   <li><strong>nexus-mmkqe</strong> made {@link PgVectorRepository#delete}'s
 *       {@code stillReferenced} anti-join TOMBSTONE-AWARE: a manifest row
 *       whose owning document is soft-tombstoned ({@code deleted_at IS NOT
 *       NULL}) now protects its chunk exactly as a live owner's row would
 *       (Tier 1, unconditional — see that method's own javadoc, "Live
 *       INCLUDES tombstoned owners"). The predecessor's Order 45 asserted
 *       the OPPOSITE — that a tombstoned referrer's manifest row did NOT
 *       protect its chunk — as a documented, dated, SKIPped gap keyed to
 *       nexus-k1uw5. That gap is CLOSED. Order 45 below asserts
 *       PROTECTION now: the shared chunk SURVIVES a delete call naming it,
 *       because the tombstoned second document's manifest row still
 *       counts.</li>
 *   <li><strong>nexus-rnqbw</strong> requires the THREE same-call
 *       tombstone-then-delete Python callers to explicitly RETRACT their
 *       own manifest row before tombstoning (see {@code store_hook.py}'s
 *       {@code _retract_manifest_rows_for_chash}). That is Python-side
 *       coverage (already shipped: {@code
 *       tests/test_rnqbw_same_call_reap_ordering.py}, {@code
 *       tests/test_o8dil5_expire_manifest_reap.py}) and is NOT duplicated
 *       here — this class stays scoped to the engine-internal Java
 *       producers plus the ONE Python composition gap
 *       ({@code test_o8dil7_prune_misclassified_manifest_antijoin_engine
 *       .py}) that a Java-only suite structurally cannot see.</li>
 * </ol>
 *
 * <p>GATE-2's criterion, per the definition of record (T2 {@code
 * nexus/rdr-191-dangling-definition-of-record} [22364], Hal's 2026-08-12
 * ruling): a {@code catalog_document_chunks} row is DANGLING (definition
 * class (a), the only class this criterion is about) when its owning
 * document is LIVE ({@code deleted_at IS NULL}) and its {@code
 * (tenant_id, collection, chash)} triple resolves in NO {@code nexus.chunks}
 * row (RDR-191 Phase 4 unified; formerly {@code chunks_384/768/1024}). A row
 * whose owner is TOMBSTONED (class b, soft-tombstone
 * residue) is NOT dangling by design — it is protected by Tier 1 above and
 * reaped later, together with its owner, by {@code purge_trash}'s
 * grace-window sweep (Tier 2). {@link #globalDanglingCount} below encodes
 * EXACTLY this — the live-owner join is load-bearing, not decorative;
 * dropping it would count legitimate tombstone residue as a false
 * producer.
 *
 * <p><strong>Anti-join key: {@code (tenant_id, collection, chash)}</strong>
 * — deliberately NOT {@link dev.nexus.service.db.ChashSqlIdioms
 * #danglingManifestCountDsl}, which is chash-only (no tenant, no
 * collection predicate) and is the exact shape that understated the live
 * corpus census by 2.2x (T2 {@code nexus/rdr-191-dangling-definition-of-
 * record} [22364]'s reconciliation section). <strong>Also deliberately NOT
 * built on {@code nexus.manifest_verify}/{@code manifest_verify_all} or
 * {@code manifest_orphans}</strong> — this tripwire must survive RDR-191
 * Phase 6 deleting those functions (the plan's own retirement item), so it
 * is raw SQL, wrap-free, independent of their continued existence even
 * though it computes the identical population they do today.
 *
 * <p>Each producer test drives a REAL production code path and then
 * asserts the GLOBAL invariant returns to baseline — never a hand-written
 * anti-join simulation, never a targeted per-fixture count alone.
 *
 * <p><strong>Non-vacuity discipline</strong> (every assertion below):
 * <ol>
 *   <li>PRE-ASSERT the exercised path actually produced a comparable row.</li>
 *   <li>{@link #invertedControl_handSeededDanglingRowIsDetected()} proves
 *       the counter actually discriminates — a hand-seeded ghost row moves
 *       it by an EXACT, predicted amount, not merely "non-zero".</li>
 *   <li>A CORRELATION PIN (one unrelated live chunk per dim, precedent
 *       {@code RekeyOpsIntegrationTest.java:1037-1071}) accompanies the
 *       inverted control so a de-correlated rewrite of the anti-join (e.g.
 *       "the chunk tables are empty") cannot pass by accident.</li>
 * </ol>
 *
 * <p>Harness: the ~40-line hand-rolled recipe every integration test in
 * this package copies (no base class / JUnit extension exists —
 * {@code StagingPromoteOpsIntegrationTest.java}). {@code
 * SharedCluster.acquireDatabase()} (via {@link PgContainerHelper#start()})
 * clones a per-CLASS database from a migrated template but reuses the
 * CLUSTER (and its roles) across every test class in the fork — role
 * creation MUST be idempotent, which is why the {@code DO $$ ... IF NOT
 * EXISTS} block below exists. Runs UNTAGGED ({@code service/pom.xml}
 * excludes {@code @Tag("integration")} from the default {@code mvn test}
 * gate) so this tripwire is live by default.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
@TestMethodOrder(MethodOrderer.OrderAnnotation.class)
class RdrO8dil7GlobalManifestAntiJoinTest {

    private static final String SVC_ROLE = "svc_o8dil7_gate2";
    private static final String SVC_PASS = "svc_o8dil7_gate2_pass";
    private static final String TENANT = "gate2-tripwire";

    PostgreSQLContainer<?> pg;
    HikariDataSource svcDs;
    TenantScope scope;
    CatalogRepository catalogRepo;
    PgVectorRepository vectorRepo;
    StagingPromoteOps promoteOps;

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.bootstrapServiceRole(su, SVC_ROLE, SVC_PASS);
            // The two gc_* function EXECUTE grants are not part of
            // bootstrapServiceRole's fixed grant set (nexus-cbo4a batch 1a) --
            // kept as explicit grants.
            su.createStatement().execute(
                "GRANT EXECUTE ON FUNCTION nexus.gc_quarantine_orphans(int, text, text, text, text, int) TO " + SVC_ROLE);
            su.createStatement().execute(
                "GRANT EXECUTE ON FUNCTION nexus.gc_expire_quarantine(int, text, text, text, text, float8, int, boolean) TO " + SVC_ROLE);
        }
        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(SVC_ROLE);
        cfg.setPassword(SVC_PASS);
        cfg.setMaximumPoolSize(5);
        cfg.setAutoCommit(true);
        svcDs = new HikariDataSource(cfg);
        scope = new TenantScope(svcDs);
        catalogRepo = new CatalogRepository(scope);
        var embedder = new ConstantEmbedder(1024);
        vectorRepo = new PgVectorRepository(scope, embedder, embedder);
        promoteOps = new StagingPromoteOps(scope);
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    /** Minimal {@link Embedder}: every text embeds to the same fixed unit vector. */
    private static final class ConstantEmbedder implements Embedder {
        private final int dim;
        ConstantEmbedder(int dim) { this.dim = dim; }

        @Override
        public List<float[]> embed(List<String> texts) {
            float[] v = new float[dim];
            v[0] = 1.0f;
            return texts.stream().map(t -> v.clone()).toList();
        }

        @Override
        public void close() { }
    }

    // ── THE TRIPWIRE ITSELF ───────────────────────────────────────────────

    /**
     * Definition-(a)-scoped ({@code d.deleted_at IS NULL}), {@code
     * (tenant_id, collection, chash)}-keyed. A manifest row is dangling by
     * GATE-2's criterion ONLY when its owning document is LIVE and NO
     * chunk table has a row for its tenant+collection+chash. A tombstoned
     * owner's manifest row (class b, soft-tombstone residue) is
     * DELIBERATELY excluded by the join — see this class's own javadoc.
     * Spans the whole per-class database — safe because {@code
     * SharedCluster.acquireDatabase()} clones a fresh database per test
     * class, so nothing outside this class's own tests can contribute rows.
     */
    private int globalDanglingCount(Connection su) throws Exception {
        try (var st = su.createStatement()) {
            var rs = st.executeQuery(
                "SELECT COUNT(*) FROM nexus.catalog_document_chunks m "
                + "JOIN nexus.catalog_documents d "
                + "  ON d.tenant_id = m.tenant_id AND d.tumbler = m.doc_id "
                + "WHERE d.deleted_at IS NULL "
                // RDR-191 Phase 4: chunks_384/768/1024 unified into ONE nexus.chunks --
                // one NOT EXISTS now covers what three ANDed ones did.
                + "AND NOT EXISTS (SELECT 1 FROM " + DimTables.CHUNKS_TABLE_NAME + " c "
                + "  WHERE c.tenant_id = m.tenant_id AND c.collection = m.collection AND c.chash = m.chash)");
            rs.next();
            return rs.getInt(1);
        }
    }

    /**
     * RAW / owner-tombstone-BLIND twin of {@link #globalDanglingCount}, used
     * ONLY by {@link #negativeControl_tombstonedOwnerGenuineGhostRow_excludedFromDefinitionA}
     * (substantive-critic Critical, T2 {@code
     * nexus/critique-gate2-tripwire-7ygt0-2026-08-13} [22388]) to prove the
     * {@code d.deleted_at IS NULL} predicate — not fixture emptiness or a
     * miscount elsewhere — is what excludes class-(b) tombstone residue.
     * IDENTICAL to {@link #globalDanglingCount} except for the missing
     * live-owner filter — this is deliberately the (a)∪(b) shape T2 {@code
     * nexus/rdr-191-dangling-definition-of-record} [22364] documents as the
     * exact source of the historical 2,951/6,501-vs-37 confusion. NOT used
     * by any producer test's own pass/fail assertion — a producer test
     * asserting against this shape would be asserting the WRONG definition
     * for GATE-2's criterion.
     */
    private int globalDanglingCountAnyOwnerState(Connection su) throws Exception {
        try (var st = su.createStatement()) {
            var rs = st.executeQuery(
                "SELECT COUNT(*) FROM nexus.catalog_document_chunks m "
                + "JOIN nexus.catalog_documents d "
                + "  ON d.tenant_id = m.tenant_id AND d.tumbler = m.doc_id "
                // RDR-191 Phase 4: chunks_384/768/1024 unified into ONE nexus.chunks --
                // one NOT EXISTS now covers what three ANDed ones did.
                + "WHERE NOT EXISTS (SELECT 1 FROM " + DimTables.CHUNKS_TABLE_NAME + " c "
                + "  WHERE c.tenant_id = m.tenant_id AND c.collection = m.collection AND c.chash = m.chash)");
            rs.next();
            return rs.getInt(1);
        }
    }

    // ── fixture helpers ───────────────────────────────────────────────────

    private static String digestHex(String text) {
        try {
            return java.util.HexFormat.of().formatHex(
                MessageDigest.getInstance("SHA-256").digest(text.getBytes(StandardCharsets.UTF_8)));
        } catch (Exception e) {
            throw new IllegalStateException(e);
        }
    }

    /** Returns a QUOTED vector literal, e.g. {@code '[0.1,0.1]'} — ready to
     *  splice directly before {@code ::vector} with no extra quoting needed
     *  at call sites. */
    private static String vec(int dim) {
        return IntStream.range(0, dim).mapToObj(i -> "0.1").collect(Collectors.joining(",", "'[", "]'"));
    }

    /**
     * A 32-char literal (not a real hex-decoded digest) used ONLY for raw-SQL
     * fixtures where both the {@code chunks_*} row and the manifest row are
     * inserted with the identical literal — bytea's escape-format input
     * accepts it verbatim on both sides, so equality holds regardless of
     * "real" hex meaning. Same idiom as {@code
     * CatalogDeleteCollectionCascadeTest#chash(String)}.
     */
    private static String chashLiteral(String seed) {
        return (seed.replaceAll("[^0-9a-f]", "a") + "0".repeat(32)).substring(0, 32);
    }

    private static int rows(Connection su, String sql) throws Exception {
        var rs = su.createStatement().executeQuery(sql);
        rs.next();
        return rs.getInt(1);
    }

    /** RDR-191 Phase 4: chunks_<dim> unified into nexus.chunks -- (collection, chash)
     *  is the full PK, so no dim/embedding-column filter is needed here. */
    private static String chunkText(Connection su, String collection, String chashHex) throws Exception {
        var rs = su.createStatement().executeQuery(
            "SELECT chunk_text FROM " + DimTables.CHUNKS_TABLE_NAME + " WHERE collection = '" + collection
            + "' AND chash = decode('" + chashHex + "', 'hex')");
        return rs.next() ? rs.getString(1) : null;
    }

    // ── Order 10: UPSERT arm — CatalogRepository.writeManifest ────────────

    @Test
    @Order(10)
    void upsertPath_writeManifest_producesNoDanglingRows() throws Exception {
        String coll = "code__gate2-upsert__voyage-code-3__v1";
        // chash MUST be the actual digest of the stored text — StagingPromoteOps
        // .finalizeTenant's in-txn residual-mismatch check (invoked by the promote
        // arm test below, same tenant) scans every chunks_* row for this tenant
        // and fails loud on chash != sha256(chunk_text).
        String content = "upsert content";
        String chash = Chash.ofText(content).toHex();

        vectorRepo.upsertChunks(TENANT, coll, List.of(chash), List.of(content),
            List.of(Map.of("title", "gate2 upsert")));
        catalogRepo.upsertDocument(TENANT, Map.of(
            "tumbler", "gate2-upsert-doc",
            "title", "gate2 upsert fixture",
            "content_type", "code",
            "corpus", "code",
            "physical_collection", coll));
        catalogRepo.writeManifest(TENANT, "gate2-upsert-doc", coll,
            List.of(Map.of("position", 0, "chash", chash, "chunk_index", 0)));

        try (Connection su = pg.createConnection("")) {
            assertThat(rows(su, "SELECT COUNT(*) FROM nexus.catalog_document_chunks "
                + "WHERE doc_id = 'gate2-upsert-doc'"))
                .as("precondition: the upsert path did not produce a comparable manifest row "
                    + "— the anti-join below would pass on an empty set")
                .isGreaterThan(0);
            assertThat(globalDanglingCount(su))
                .as("upsert path (writeManifest): zero dangling manifest rows").isZero();
        }
    }

    // ── Order 20: PROMOTE arm — StagingPromoteOps.finalizeTenant (F12b) ────

    @Test
    @Order(20)
    void promotePath_finalizeTenant_producesNoDanglingRows() throws Exception {
        String coll = "knowledge__gate2-promote__bge-base-en-v15-768__v1";
        String text = "gate2 promote content " + System.nanoTime();
        String canonical = digestHex(text);

        scope.withTenant(TENANT, ctx -> {
            ctx.execute("INSERT INTO staging.chunks "
                + "(tenant_id, collection, dim, legacy_ref, chunk_text, embedding, model) "
                + "VALUES (?, ?, 768, ?, ?, " + vec(768) + "::vector, 'bge-768')",
                TENANT, coll, canonical, text);
            return null;
        });
        Map<String, Object> promoted = promoteOps.promoteCollection(TENANT, coll, 768);
        assertThat(promoted.get("promoted"))
            .as("precondition: the staged chunk actually landed content").isEqualTo(1);

        catalogRepo.upsertDocument(TENANT, Map.of(
            "tumbler", "gate2-promote-doc",
            "title", "gate2 promote fixture",
            "content_type", "knowledge",
            "corpus", "knowledge",
            "physical_collection", coll));
        scope.withTenant(TENANT, ctx -> {
            ctx.execute("INSERT INTO staging.document_chunks (tenant_id, doc_id, position, chash) "
                + "VALUES (?, 'gate2-promote-doc', 0, ?)", TENANT, canonical);
            return null;
        });

        Map<String, Object> fin = promoteOps.finalizeTenant(TENANT, false);
        assertThat(fin.get("manifest_promoted"))
            .as("precondition: finalizeTenant actually promoted a manifest row — the anti-join "
                + "below would pass on an empty set")
            .isEqualTo(1);

        try (Connection su = pg.createConnection("")) {
            assertThat(globalDanglingCount(su))
                .as("promote path (finalizeTenant): zero dangling manifest rows").isZero();
        }
    }

    // ── Order 30: DELETE arm — CatalogRepository.deleteCollection (F8d) ────

    @Test
    @Order(30)
    void deletePath_deleteCollection_scopingAsymmetry_producesNoDanglingRows() throws Exception {
        String collHome = "knowledge__gate2-del-home__voyage-context-3__v1";
        String collDel = "knowledge__gate2-del-victim__voyage-context-3__v1";
        String chash = chashLiteral("gate2-del-asym-content");

        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            su.createStatement().execute("INSERT INTO nexus.catalog_collections (tenant_id, name) "
                + "VALUES ('" + TENANT + "', '" + collHome + "')");
            su.createStatement().execute("INSERT INTO nexus.catalog_collections (tenant_id, name) "
                + "VALUES ('" + TENANT + "', '" + collDel + "')");
            // Document homed in collHome; its manifest row's OWN denormalized
            // `collection` names collDel (F8c reclassification-without-reindex
            // drift shape) — reachable by NEITHER scope symmetrically before
            // the F8d fix.
            su.createStatement().execute("INSERT INTO nexus.catalog_documents "
                + "(tenant_id, tumbler, title, physical_collection) "
                + "VALUES ('" + TENANT + "', 'gate2-del-doc', 'gate2 del doc', '" + collHome + "')");
            su.createStatement().execute("INSERT INTO " + DimTables.CHUNKS_TABLE_NAME + " "
                + "(tenant_id, collection, chash, chunk_text, " + DimTables.embeddingColumn(1024) + ") "
                + "VALUES ('" + TENANT + "', '" + collDel + "', '" + chash + "', 'text', "
                + vec(1024) + "::vector)");
            su.createStatement().execute("INSERT INTO nexus.catalog_document_chunks "
                + "(tenant_id, doc_id, position, chash, collection) "
                + "VALUES ('" + TENANT + "', 'gate2-del-doc', 0, '" + chash + "', '" + collDel + "')");
        }
        try (Connection su = pg.createConnection("")) {
            assertThat(rows(su, "SELECT COUNT(*) FROM nexus.catalog_document_chunks "
                + "WHERE doc_id = 'gate2-del-doc'"))
                .as("precondition: the asymmetry manifest row was actually seeded")
                .isEqualTo(1);
        }

        catalogRepo.deleteCollection(TENANT, collDel);

        try (Connection su = pg.createConnection("")) {
            assertThat(globalDanglingCount(su))
                .as("delete path (deleteCollection, F8d asymmetry): zero dangling manifest rows")
                .isZero();
        }
    }

    // ── Order 40: DELETE arm — PgVectorRepository.delete still-referenced (F10c) ──

    @Test
    @Order(40)
    void deletePath_stillReferencedByLiveDocProtected_producesNoDanglingRows() throws Exception {
        String coll = "code__gate2-del-shared__voyage-code-3__v1";
        String sharedText = "shared content";
        String shared = Chash.ofText(sharedText).toHex();

        vectorRepo.upsertChunks(TENANT, coll, List.of(shared), List.of(sharedText),
            List.of(Map.of("title", "shared")));
        catalogRepo.upsertDocument(TENANT, Map.of(
            "tumbler", "gate2-del-doc-a", "title", "a",
            "content_type", "code", "corpus", "code", "physical_collection", coll));
        catalogRepo.upsertDocument(TENANT, Map.of(
            "tumbler", "gate2-del-doc-b", "title", "b",
            "content_type", "code", "corpus", "code", "physical_collection", coll));
        catalogRepo.writeManifest(TENANT, "gate2-del-doc-a", coll,
            List.of(Map.of("position", 0, "chash", shared, "chunk_index", 0)));
        catalogRepo.writeManifest(TENANT, "gate2-del-doc-b", coll,
            List.of(Map.of("position", 0, "chash", shared, "chunk_index", 0)));
        // Realistic ordering (matches every funnel caller but F8c): doc A's OWN
        // manifest is cleared BEFORE the physical chunk delete call.
        catalogRepo.writeManifest(TENANT, "gate2-del-doc-a", coll, List.of());

        try (Connection su = pg.createConnection("")) {
            assertThat(rows(su, "SELECT COUNT(*) FROM nexus.catalog_document_chunks "
                + "WHERE doc_id = 'gate2-del-doc-b'"))
                .as("precondition: doc B's manifest still references the shared chash "
                    + "— the anti-join below would pass on an empty set")
                .isEqualTo(1);
        }

        // Caller deletes "document A's former chunks", including the chash it
        // happens to share with document B (F10c premise: the caller cannot
        // know the sharing).
        vectorRepo.delete(TENANT, coll, List.of(shared));

        try (Connection su = pg.createConnection("")) {
            assertThat(chunkText(su, coll, shared))
                .as("F10c: a chunk another LIVE document's manifest still references must "
                    + "physically survive delete")
                .isEqualTo(sharedText);
            assertThat(globalDanglingCount(su))
                .as("delete path (PgVectorRepository.delete, F10c still-referenced): "
                    + "zero dangling manifest rows").isZero();
        }
    }

    // ── Order 45: DELETE arm — PgVectorRepository.delete, TOMBSTONED referrer
    //    (nexus-mmkqe Tier 1 — now UNCONDITIONAL protection) ────────────────

    /**
     * nexus-mmkqe (RDR-191 GATE-2 delete-path quartet, 2026-08-12): {@link
     * PgVectorRepository#delete}'s {@code stillReferenced} anti-join no
     * longer carries a {@code deleted_at IS NULL} predicate at all — a
     * TOMBSTONED referrer's manifest row protects its chunk exactly as a
     * live referrer's would (Tier 1, unconditional; see that method's own
     * "TWO-TIER PROTECTION" javadoc section). This supersedes the
     * predecessor tripwire's Order 45, which asserted the OPPOSITE as a
     * documented, dated SKIP keyed to nexus-k1uw5 — that gap is now
     * CLOSED, so this is a genuine, unconditional PASS assertion, not a
     * forced skip.
     *
     * <p>Same call shape as Order 40 above, but the second referrer is
     * TOMBSTONED (via the real {@code deleteDocument} soft-tombstone path,
     * which deliberately leaves {@code catalog_document_chunks} in place —
     * {@code store_hook.py}'s documented soft-tombstone contract) rather
     * than left live.
     */
    @Test
    @Order(45)
    void deletePath_stillReferencedByTombstonedDocProtected_producesNoDanglingRows() throws Exception {
        String coll = "code__gate2-del-tombstone__voyage-code-3__v1";
        String sharedText = "shared content tombstoned";
        String shared = Chash.ofText(sharedText).toHex();

        vectorRepo.upsertChunks(TENANT, coll, List.of(shared), List.of(sharedText),
            List.of(Map.of("title", "shared-tombstone")));
        catalogRepo.upsertDocument(TENANT, Map.of(
            "tumbler", "gate2-del-tomb-doc-a", "title", "a",
            "content_type", "code", "corpus", "code", "physical_collection", coll));
        catalogRepo.upsertDocument(TENANT, Map.of(
            "tumbler", "gate2-del-tomb-doc-b", "title", "b",
            "content_type", "code", "corpus", "code", "physical_collection", coll));
        catalogRepo.writeManifest(TENANT, "gate2-del-tomb-doc-a", coll,
            List.of(Map.of("position", 0, "chash", shared, "chunk_index", 0)));
        catalogRepo.writeManifest(TENANT, "gate2-del-tomb-doc-b", coll,
            List.of(Map.of("position", 0, "chash", shared, "chunk_index", 0)));
        catalogRepo.writeManifest(TENANT, "gate2-del-tomb-doc-a", coll, List.of());

        // The referring document is TOMBSTONED via the REAL production path
        // (CatalogRepository.deleteDocument — sets deleted_at = NOW(), does
        // NOT touch catalog_document_chunks), not a raw UPDATE.
        catalogRepo.deleteDocument(TENANT, "gate2-del-tomb-doc-b");

        try (Connection su = pg.createConnection("")) {
            assertThat(rows(su, "SELECT COUNT(*) FROM nexus.catalog_document_chunks "
                + "WHERE doc_id = 'gate2-del-tomb-doc-b'"))
                .as("precondition: the tombstoned doc's manifest row still references the "
                    + "shared chash — soft-tombstone deliberately leaves it in place")
                .isEqualTo(1);
            assertThat(rows(su, "SELECT COUNT(*) FROM nexus.catalog_documents "
                + "WHERE tumbler = 'gate2-del-tomb-doc-b' AND deleted_at IS NOT NULL"))
                .as("precondition: doc B is actually tombstoned").isEqualTo(1);
        }

        // Caller deletes "document A's former chunks" — same call shape as
        // Order 40, but this time the OTHER referrer is tombstoned rather
        // than live.
        vectorRepo.delete(TENANT, coll, List.of(shared));

        try (Connection su = pg.createConnection("")) {
            assertThat(chunkText(su, coll, shared))
                .as("nexus-mmkqe Tier 1: a chunk a TOMBSTONED document's manifest row still "
                    + "names must physically survive delete, unconditionally — the "
                    + "soft-tombstone contract deliberately leaves that reference in place, "
                    + "and independent deleters must not destroy content it still names")
                .isEqualTo(sharedText);
            assertThat(globalDanglingCount(su))
                .as("delete path (PgVectorRepository.delete, tombstoned referrer): zero "
                    + "dangling manifest rows — doc B's own row is class-(b) tombstone "
                    + "residue, excluded by definition, not a producer")
                .isZero();
        }
    }

    // ── Order 50: GC quarantine path — nexus.gc_quarantine_orphans ─────────

    /**
     * nexus-c30ew (P0, CLOSED as an ARTIFACT of the rejected
     * collection-inference design, 2026-08-12): the predecessor tripwire's
     * Orders 50/55/60 forced a manifest row's {@code collection} to NULL
     * to reproduce the NULL-comparison laundering bug in {@code
     * gc_quarantine_orphans} / {@code gc_restore_rereferenced}. That state
     * is UNREPRESENTABLE post-catalog-025 ({@code collection} is {@code
     * NOT NULL}) — a raw {@code UPDATE ... SET collection = NULL} now
     * fails at the database with a NOT NULL violation, not a graceful
     * "reproduces the bug" outcome. {@code m.collection = c.collection} is
     * simply CORRECT once {@code collection} is a caller-supplied verified
     * fact rather than an inferred nullable — the bead's own close note.
     * The rescue-path arm ({@code gc_restore_rereferenced}, the
     * predecessor's Order 55) is DROPPED entirely: it existed solely to
     * prove the identical NULL-comparison defect on the rescue path, which
     * is equally unrepresentable now.
     *
     * <p>This test therefore covers the ORDINARY case with real,
     * correctly-stamped collections: a manifest-referenced chunk MUST
     * survive {@code quarantineOrphans}, and a genuinely unreferenced
     * (orphan) chunk in the SAME call MUST be moved — the in-run control
     * that proves the survival assertion can observe a genuine PASS, not
     * merely be wired to always succeed. The GATE-2-specific value here
     * (beyond {@code PgVectorRepositoryGcQuarantineTest}'s already-thorough
     * per-chunk coverage of this SQL function) is the GLOBAL
     * definition-(a) dangling invariant returning to baseline through the
     * REAL production entry point — the composability check this whole
     * class exists for.
     */
    @Test
    @Order(50)
    void gcQuarantinePath_manifestReferencedChunkProtected_orphanReclaimed_producesNoDanglingRows()
        throws Exception {
        String coll = "code__gate2-gc-quarantine__voyage-code-3__v1";
        String quarantineColl = coll + "__quarantine";
        String protectedContent = "gate2-gc-protected-content";
        String protectedChash = Chash.ofText(protectedContent).toHex();
        String orphanContent = "gate2-gc-orphan-content";
        String orphanChash = Chash.ofText(orphanContent).toHex();

        vectorRepo.upsertChunks(TENANT, coll, List.of(protectedChash, orphanChash),
            List.of(protectedContent, orphanContent),
            List.of(Map.of("title", "gc protected fixture"), Map.of("title", "gc orphan fixture")));
        catalogRepo.upsertDocument(TENANT, Map.of(
            "tumbler", "gate2-gc-protected-doc", "title", "gc protected doc",
            "content_type", "code", "corpus", "code", "physical_collection", coll));
        catalogRepo.writeManifest(TENANT, "gate2-gc-protected-doc", coll,
            List.of(Map.of("position", 0, "chash", protectedChash, "chunk_index", 0)));
        // orphanChash deliberately has NO manifest row anywhere — the
        // ordinary orphan shape.

        try (Connection su = pg.createConnection("")) {
            assertThat(chunkText(su, coll, protectedChash))
                .as("precondition: the protected chunk physically exists before GC runs")
                .isEqualTo(protectedContent);
            assertThat(chunkText(su, coll, orphanChash))
                .as("precondition: the orphan chunk physically exists before GC runs")
                .isEqualTo(orphanContent);
        }

        var outcome = vectorRepo.quarantineOrphans(TENANT, coll, quarantineColl,
            "2026-08-13T00:00:00Z", 10);
        assertThat(outcome.moved())
            .as("precondition: quarantineOrphans actually moved the orphan — the survival "
                + "checks below would be trivially satisfied on a zero-op call")
            .isEqualTo(1L);

        try (Connection su = pg.createConnection("")) {
            // In-run control: the manifest-referenced chunk MUST still be in
            // the ORIGIN collection — proves the survival check below can
            // observe a genuine PASS, not merely be wired to always succeed.
            assertThat(chunkText(su, coll, protectedChash))
                .as("control: a manifest-referenced chunk must survive quarantineOrphans in "
                    + "the SAME call that quarantines a genuine orphan — proves the survival "
                    + "check can discriminate")
                .isEqualTo(protectedContent);
            assertThat(chunkText(su, coll, orphanChash))
                .as("the genuine orphan must actually be moved out of the origin collection")
                .isNull();
            assertThat(chunkText(su, quarantineColl, orphanChash))
                .as("the genuine orphan must land in the quarantine collection")
                .isEqualTo(orphanContent);
            assertThat(globalDanglingCount(su))
                .as("gc quarantine path (gc_quarantine_orphans): zero dangling manifest rows")
                .isZero();
        }
    }

    // ── Order 60: GC expire path — nexus.gc_expire_quarantine (nexus-wnpet) ─

    /**
     * nexus-wnpet (RDR-191 GATE-2 delete-path quartet): {@code
     * gc_expire_quarantine} now refuses (never hard-deletes, never
     * overridable by {@code force}) a past-cutoff quarantined chash still
     * named by a manifest row in the origin collection — see
     * catalog-027's header. {@code
     * PgVectorRepositoryGcQuarantineTest#expireQuarantine_manifestReferencedChunk_isRefusedNotExpired_survivesEvenWithForce}
     * already proves this at the SQL-function level in detail; this test's
     * OWN value is the GATE-2 composability check — driving the same
     * production entry point and confirming the GLOBAL definition-(a)
     * dangling invariant holds, not re-deriving that function's per-chunk
     * correctness.
     *
     * <p>Scenario: a chash already physically quarantined (simulating a
     * chunk {@code gc_quarantine_orphans} moved on an earlier pass) whose
     * ORIGIN-collection manifest row still names it — the shape
     * catalog-027's own header describes ("a heal re-referenced it after
     * the cutoff was already computed"). An in-run control (a second
     * quarantined-but-unreferenced chash, past the SAME cutoff) proves the
     * "expired" outcome can be observed too, so the refusal assertion is
     * not merely wired to always report "refused".
     */
    @Test
    @Order(60)
    void gcExpirePath_manifestReferencedChunkRefused_unreferencedExpires_producesNoDanglingRows()
        throws Exception {
        String coll = "code__gate2-gc-expire__voyage-code-3__v1";
        String quarantineColl = coll + "__quarantine";
        String refContent = "gate2-gc-expire-referenced-content";
        String refChash = Chash.ofText(refContent).toHex();
        String unrefContent = "gate2-gc-expire-unreferenced-content";
        String unrefChash = Chash.ofText(unrefContent).toHex();
        String pastCutoff = "2020-01-01T00:00:00Z";
        String cutoff = "2026-01-01T00:00:00Z";

        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            su.createStatement().execute("INSERT INTO nexus.catalog_collections (tenant_id, name) "
                + "VALUES ('" + TENANT + "', '" + coll + "')");
            su.createStatement().execute("INSERT INTO nexus.catalog_collections (tenant_id, name) "
                + "VALUES ('" + TENANT + "', '" + quarantineColl + "')");
            for (var pair : List.of(Map.entry(refChash, refContent), Map.entry(unrefChash, unrefContent))) {
                su.createStatement().execute("INSERT INTO " + DimTables.CHUNKS_TABLE_NAME + " "
                    + "(tenant_id, collection, chash, chunk_text, " + DimTables.embeddingColumn(1024) + ", metadata) VALUES ('"
                    + TENANT + "', '" + quarantineColl + "', decode('" + pair.getKey() + "', 'hex'), '"
                    + pair.getValue() + "', " + vec(1024) + "::vector, jsonb_build_object("
                    + "'origin_collection', '" + coll + "', 'quarantined_at', '" + pastCutoff + "'))");
            }
        }
        catalogRepo.upsertDocument(TENANT, Map.of(
            "tumbler", "gate2-gc-expire-doc", "title", "gc expire doc",
            "content_type", "code", "corpus", "code", "physical_collection", coll));
        // RDR-191 Phase 5 (nexus-o8dil.29): this manifest row is dangling BY
        // CONSTRUCTION (refChash's only physical backing lives in
        // quarantineColl, never in coll -- see the class javadoc above), so
        // fk_catalog_chunks_chunk would refuse this writeManifest call outright.
        // Bypass the FK locally around this one Java-level write: drop the
        // constraint, write, then re-add it NOT VALID (catalog-029-0's exact
        // shape) so it is live again (unvalidated) for the rest of this test.
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            su.createStatement().execute(
                "ALTER TABLE nexus.catalog_document_chunks DROP CONSTRAINT IF EXISTS fk_catalog_chunks_chunk");
        }
        catalogRepo.writeManifest(TENANT, "gate2-gc-expire-doc", coll,
            List.of(Map.of("position", 0, "chash", refChash, "chunk_index", 0)));
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            su.createStatement().execute(
                "ALTER TABLE nexus.catalog_document_chunks "
                + "ADD CONSTRAINT fk_catalog_chunks_chunk "
                + "FOREIGN KEY (tenant_id, collection, chash) REFERENCES nexus.chunks (tenant_id, collection, chash) "
                + "ON UPDATE CASCADE DEFERRABLE INITIALLY IMMEDIATE NOT VALID");
        }
        // unrefChash deliberately has NO manifest row anywhere.

        int danglingBefore;
        try (Connection su = pg.createConnection("")) {
            assertThat(chunkText(su, quarantineColl, refChash))
                .as("precondition: the referenced chunk sits in quarantine, past its cutoff")
                .isEqualTo(refContent);
            assertThat(chunkText(su, quarantineColl, unrefChash))
                .as("precondition: the unreferenced chunk sits in quarantine, past its cutoff")
                .isEqualTo(unrefContent);
            // The fixture's own manifest row (collection = coll, the ORIGIN
            // collection) is dangling BY CONSTRUCTION from the moment it is
            // seeded, independent of this test's assertions: its physical
            // backing lives only in quarantineColl, never in coll, in
            // EITHER outcome (refused-and-preserved-in-quarantine, or
            // hard-deleted). globalDanglingCount is origin-collection-scoped
            // (keyed on m.collection) and so cannot observe which of those
            // two outcomes occurred — the discriminating assertion for
            // nexus-wnpet is the PHYSICAL survival check below, not this
            // count. Captured here so the invariant assert after the call
            // is a genuine "the call did not make it WORSE" delta check,
            // not a vacuous absolute-zero claim this fixture can never
            // satisfy.
            danglingBefore = globalDanglingCount(su);
        }

        // force=true — proves the manifest-reference refusal is NOT
        // overridable by force (nexus-wnpet), unlike the grace-window
        // floor refusal it shares a return population with.
        var outcome = vectorRepo.expireQuarantine(TENANT, quarantineColl, coll, cutoff, 1.0, 0, true);

        try (Connection su = pg.createConnection("")) {
            // In-run control: the genuinely unreferenced chash must
            // actually expire — proves the "expired" outcome is
            // observable, not just a permanent "refused" report.
            assertThat(chunkText(su, quarantineColl, unrefChash))
                .as("control: a genuinely unreferenced past-cutoff chunk must actually expire "
                    + "in the SAME call — proves the outcome can discriminate")
                .isNull();
            assertThat(chunkText(su, quarantineColl, refChash))
                .as("nexus-wnpet: a past-cutoff chunk a manifest row still references must "
                    + "NOT be hard-deleted, even with force=true")
                .isEqualTo(refContent);
            assertThat(outcome.refused())
                .as("the manifest-referenced chash must be counted as refused, not silently "
                    + "dropped from the return population")
                .isGreaterThanOrEqualTo(1L);
            assertThat(globalDanglingCount(su))
                .as("gc expire path (gc_expire_quarantine): the call must not WORSEN the "
                    + "definition-(a) dangling count relative to its pre-existing (fixture-"
                    + "seeded) baseline")
                .isEqualTo(danglingBefore);
        }
    }

    // ── Order 80: NEGATIVE CONTROL — class-(b) tombstone exclusion ─────────

    /**
     * substantive-critic Critical finding (T2 {@code
     * nexus/critique-gate2-tripwire-7ygt0-2026-08-13} [22388]): {@link
     * #globalDanglingCount}'s {@code d.deleted_at IS NULL} predicate — the
     * SINGLE load-bearing distinction T2 {@code
     * nexus/rdr-191-dangling-definition-of-record} [22364] exists to
     * establish — had ZERO kill control anywhere in this class. Order 45's
     * tombstoned-referrer fixture does not exercise it either: that chunk
     * is PHYSICALLY PROTECTED (never deleted), so it reads as non-dangling
     * whether or not the predicate is present — a regression to the raw
     * (a)∪(b) shape would not have turned Order 45 red.
     *
     * <p>This is the true negative-direction twin of {@link
     * #invertedControl_handSeededDanglingRowIsDetected} (Order 90): it
     * hand-seeds a manifest row whose OWNER IS TOMBSTONED (via the real
     * {@code deleteDocument} soft-tombstone path, matching Order 45's own
     * discipline) and whose {@code (tenant_id, collection, chash)}
     * genuinely has NO backing chunk row anywhere — a genuine class-(b)
     * row, per T2 [22364]'s three-way partition, distinct from Order 90's
     * class-(a) ghost (live owner).
     *
     * <p>Asserts BOTH directions in one test: {@link #globalDanglingCount}
     * (definition-(a)-scoped) must NOT move — proves the exclusion fires —
     * while {@link #globalDanglingCountAnyOwnerState} (the raw,
     * tombstone-blind twin) on the SAME database state WOULD see it —
     * proves the exclusion itself, not fixture emptiness or a miscount
     * elsewhere, is what is doing the discriminating work. A counter that
     * structurally cannot see ANYTHING would also read "unchanged" on the
     * first assertion alone, which is exactly the false-pass the second
     * assertion rules out.
     */
    @Test
    @Order(80)
    void negativeControl_tombstonedOwnerGenuineGhostRow_excludedFromDefinitionA() throws Exception {
        String coll = "knowledge__gate2-control-tombstone__voyage-context-3__v1";
        String ghostChash = chashLiteral("gate2-control-tombstone-ghost");

        int danglingBefore;
        int rawBefore;
        try (Connection su = pg.createConnection("")) {
            danglingBefore = globalDanglingCount(su);
            rawBefore = globalDanglingCountAnyOwnerState(su);
        }

        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            su.createStatement().execute("INSERT INTO nexus.catalog_collections (tenant_id, name) "
                + "VALUES ('" + TENANT + "', '" + coll + "')");
            su.createStatement().execute("INSERT INTO nexus.catalog_documents "
                + "(tenant_id, tumbler, title, physical_collection) VALUES "
                + "('" + TENANT + "', 'gate2-control-tombstone-doc', 'control tombstone doc', '" + coll + "')");
            // Genuinely a class-(b) ghost: ghostChash resolves in NO chunk
            // table anywhere (same construction as Order 90's class-(a)
            // ghost) — the owner below is tombstoned, turning this into the
            // negative-direction twin.
            // RDR-191 Phase 5 (nexus-o8dil.29): fk_catalog_chunks_chunk now
            // requires a matching nexus.chunks row -- a genuinely-dangling row
            // is exactly this test's SUBJECT, so bypass the FK locally: drop
            // the constraint, insert, then re-add it NOT VALID (catalog-029-0's
            // exact shape) so it is live again (unvalidated) afterward.
            su.createStatement().execute(
                "ALTER TABLE nexus.catalog_document_chunks DROP CONSTRAINT IF EXISTS fk_catalog_chunks_chunk");
            su.createStatement().execute("INSERT INTO nexus.catalog_document_chunks "
                + "(tenant_id, doc_id, position, chash, collection) VALUES "
                + "('" + TENANT + "', 'gate2-control-tombstone-doc', 0, '" + ghostChash + "', '" + coll + "')");
            su.createStatement().execute(
                "ALTER TABLE nexus.catalog_document_chunks "
                + "ADD CONSTRAINT fk_catalog_chunks_chunk "
                + "FOREIGN KEY (tenant_id, collection, chash) REFERENCES nexus.chunks (tenant_id, collection, chash) "
                + "ON UPDATE CASCADE DEFERRABLE INITIALLY IMMEDIATE NOT VALID");
        }
        // Real production tombstone path (CatalogRepository.deleteDocument),
        // not a raw UPDATE — matches Order 45's own discipline. Soft
        // tombstone deliberately leaves catalog_document_chunks in place.
        catalogRepo.deleteDocument(TENANT, "gate2-control-tombstone-doc");

        try (Connection su = pg.createConnection("")) {
            assertThat(rows(su, "SELECT COUNT(*) FROM nexus.catalog_document_chunks "
                + "WHERE doc_id = 'gate2-control-tombstone-doc'"))
                .as("precondition: the tombstoned owner's manifest row still exists — "
                    + "soft-tombstone deliberately leaves it in place — the assertions "
                    + "below would pass vacuously on an empty set otherwise")
                .isEqualTo(1);
            assertThat(rows(su, "SELECT COUNT(*) FROM nexus.catalog_documents "
                + "WHERE tumbler = 'gate2-control-tombstone-doc' AND deleted_at IS NOT NULL"))
                .as("precondition: the owner is actually tombstoned").isEqualTo(1);

            assertThat(globalDanglingCount(su))
                .as("class-(b) EXCLUSION: a manifest row whose owner is tombstoned must NOT "
                    + "move the definition-(a) dangling counter, even though it is a genuine "
                    + "ghost (no backing chunk row anywhere) — this is GATE-2's own ruled "
                    + "definition, not an oversight")
                .isEqualTo(danglingBefore);
            assertThat(globalDanglingCountAnyOwnerState(su))
                .as("KILL CONTROL: the SAME row, read by the raw owner-tombstone-BLIND "
                    + "counter, MUST move by exactly +1 — proves the exclusion above is the "
                    + "discriminating term (not fixture emptiness, not a counter that cannot "
                    + "see anything) — a regression that deletes the d.deleted_at IS NULL "
                    + "predicate would make THIS assertion's own baseline comparison "
                    + "meaningless as a control, which is exactly why it is asserted here "
                    + "independently rather than assumed")
                .isEqualTo(rawBefore + 1);
        }
    }

    // ── Order 90: INVERTED CONTROL — proves the counter discriminates ──────

    /**
     * Hand-seeds a genuinely dangling manifest row (superuser, bypasses
     * RLS — same privilege level every fixture-seeding block in this file
     * already uses), plus a CORRELATION PIN (one unrelated LIVE chunk per
     * dim, {@code RekeyOpsIntegrationTest.java:1037-1071} precedent) so a
     * de-correlated rewrite of the anti-join (e.g. "the chunk tables are
     * empty") cannot pass by accident. Asserts an EXACT delta, not merely
     * "non-zero" — the strongest available proof that the counter used by
     * every producer test above actually discriminates a bad row from a
     * good one, rather than passing vacuously.
     */
    @Test
    @Order(90)
    void invertedControl_handSeededDanglingRowIsDetected() throws Exception {
        String coll = "knowledge__gate2-control__voyage-context-3__v1";
        String ghostChash = chashLiteral("gate2-control-ghost");
        String pin384 = chashLiteral("gate2-control-pin-384");
        String pin768 = chashLiteral("gate2-control-pin-768");
        String pin1024 = chashLiteral("gate2-control-pin-1024");

        int danglingBefore;
        try (Connection su = pg.createConnection("")) {
            danglingBefore = globalDanglingCount(su);
        }

        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            su.createStatement().execute("INSERT INTO nexus.catalog_collections (tenant_id, name) "
                + "VALUES ('" + TENANT + "', '" + coll + "')");
            // Correlation pin: one unrelated LIVE chunk per dim (three distinct rows,
            // one per embedding_<dim> column, RDR-191 unified table), in the SAME
            // collection, so the anti-join cannot pass by "nexus.chunks happens to
            // be empty".
            su.createStatement().execute("INSERT INTO " + DimTables.CHUNKS_TABLE_NAME + " "
                + "(tenant_id, collection, chash, chunk_text, " + DimTables.embeddingColumn(384) + ") VALUES "
                + "('" + TENANT + "', '" + coll + "', '" + pin384 + "', 'pin', " + vec(384) + "::vector)");
            su.createStatement().execute("INSERT INTO " + DimTables.CHUNKS_TABLE_NAME + " "
                + "(tenant_id, collection, chash, chunk_text, " + DimTables.embeddingColumn(768) + ") VALUES "
                + "('" + TENANT + "', '" + coll + "', '" + pin768 + "', 'pin', " + vec(768) + "::vector)");
            su.createStatement().execute("INSERT INTO " + DimTables.CHUNKS_TABLE_NAME + " "
                + "(tenant_id, collection, chash, chunk_text, " + DimTables.embeddingColumn(1024) + ") VALUES "
                + "('" + TENANT + "', '" + coll + "', '" + pin1024 + "', 'pin', " + vec(1024) + "::vector)");
            su.createStatement().execute("INSERT INTO nexus.catalog_documents "
                + "(tenant_id, tumbler, title, physical_collection) VALUES "
                + "('" + TENANT + "', 'gate2-control-doc', 'control doc', '" + coll + "')");
            // Genuinely dangling: ghostChash resolves in NO chunk table, and
            // the owning document is LIVE (no deleted_at set above).
            // RDR-191 Phase 5 (nexus-o8dil.29): fk_catalog_chunks_chunk now
            // requires a matching nexus.chunks row -- a genuinely-dangling row
            // is exactly this test's SUBJECT, so bypass the FK locally: drop
            // the constraint, insert, then re-add it NOT VALID (catalog-029-0's
            // exact shape) so it is live again (unvalidated) afterward.
            su.createStatement().execute(
                "ALTER TABLE nexus.catalog_document_chunks DROP CONSTRAINT IF EXISTS fk_catalog_chunks_chunk");
            su.createStatement().execute("INSERT INTO nexus.catalog_document_chunks "
                + "(tenant_id, doc_id, position, chash, collection) VALUES "
                + "('" + TENANT + "', 'gate2-control-doc', 0, '" + ghostChash + "', '" + coll + "')");
            su.createStatement().execute(
                "ALTER TABLE nexus.catalog_document_chunks "
                + "ADD CONSTRAINT fk_catalog_chunks_chunk "
                + "FOREIGN KEY (tenant_id, collection, chash) REFERENCES nexus.chunks (tenant_id, collection, chash) "
                + "ON UPDATE CASCADE DEFERRABLE INITIALLY IMMEDIATE NOT VALID");
        }

        try (Connection su = pg.createConnection("")) {
            assertThat(globalDanglingCount(su))
                .as("control: the hand-seeded ghost row must move the dangling counter by "
                    + "exactly +1 — proves the assertion discriminates rather than passing "
                    + "vacuously on an empty table")
                .isEqualTo(danglingBefore + 1);
        }
    }
}
