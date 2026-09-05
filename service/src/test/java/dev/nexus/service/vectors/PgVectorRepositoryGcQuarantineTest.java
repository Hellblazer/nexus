// SPDX-License-Identifier: AGPL-3.0-or-later
package dev.nexus.service.vectors;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.PgContainerHelper;
import dev.nexus.service.db.CatalogRepository;
import dev.nexus.service.db.Chash;
import dev.nexus.service.db.TenantScope;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.sql.SQLException;
import java.util.List;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * RDR-191 Phase 1 (bead-less; RDR §Decision item 5 / Phasing "Phase 1 — Prune
 * into SQL"): contract suite for {@code nexus.gc_quarantine_orphans} /
 * {@code gc_restore_rereferenced} / {@code gc_expire_quarantine}
 * (catalog-023-quarantine-functions.xml) and their
 * {@link PgVectorRepository} bindings.
 *
 * <p>This is the load-bearing EQUIVALENCE proof the RDR-191 brief demands:
 * the SQL anti-join (keyed on {@code chunks_<dim>.chash} — the PK column —
 * joined to {@code catalog_document_chunks.chash}) must classify exactly
 * the chunks the Python {@code _prune_deleted_files}/{@code
 * chunk_quarantine.py} pair would have classified as orphan/live/undecidable,
 * for the same corpus. Cases proven: orphans present (moved, sample
 * correct), no orphans (zero-op), the PK-collision-on-re-quarantine
 * decision (INSERT ... ON CONFLICT DO UPDATE, upsert semantics — matches
 * the Python "copy-then-delete: never lossy" upsert contract), restore of a
 * re-referenced chunk, the grace-window floor refusal AND its {@code force}
 * override, and tenant isolation (RLS). The "undecidable identity"
 * (Python's {@code unsafe_skipped}) case is proven STRUCTURALLY
 * IMPOSSIBLE here — {@code chash} is a {@code NOT NULL} PK column, so no
 * live row can lack one.
 *
 * <p>Hermetic: Testcontainers pgvector/pgvector:pg17, PER_CLASS lifecycle,
 * mirrors {@code CatalogRepositoryTest}'s bootstrap (a plain-LOGIN,
 * non-superuser {@code svc_gcq_test} role so RLS assertions are
 * non-vacuous — nexus-5j7pb class).
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class PgVectorRepositoryGcQuarantineTest {

    private static final String TENANT_A = "gcq-tenant-a";
    private static final String TENANT_B = "gcq-tenant-b";
    private static final String SVC_ROLE = "svc_gcq_test";
    private static final String SVC_PASS = "svc_gcq_test_pass";

    private static String ch(String seed) {
        return Chash.ofText(seed).toHex();
    }

    /** Per-test-case collection pair — PER_CLASS lifecycle shares one container/tenant
     *  across all methods with no rollback between them, so a shared collection name
     *  would let one test's leftover rows (e.g. a still-referenced chunk, or a row
     *  stranded by an assertion failure before cleanup) bleed into another test's
     *  orphan count. Each test gets its own collection pair, keyed on its own case name. */
    private static String originCol(String testCase) {
        return "code__gcq-" + testCase + "__voyage-code-3__v1";
    }
    private static String quarantineCol(String testCase) {
        return "quarantine-code__gcq-" + testCase + "__voyage-code-3__v1";
    }

    PostgreSQLContainer<?> pg;
    TenantScope tenantScope;
    CatalogRepository catalogRepo;
    PgVectorRepository vectorRepo;
    HikariDataSource svcDs;

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();

        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }

        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.bootstrapServiceRole(su, SVC_ROLE, SVC_PASS);
            // The three gc_* function EXECUTE grants are not part of
            // bootstrapServiceRole's fixed grant set (nexus-cbo4a batch 1a) --
            // kept as explicit grants. The named-sequence grants this block used to
            // carry (catalog_links_id_seq, gc_audit_id_seq) are now redundant with
            // bootstrapServiceRole's broader "USAGE, SELECT ON ALL SEQUENCES" and
            // are dropped.
            su.createStatement().execute(
                "GRANT EXECUTE ON FUNCTION nexus.gc_quarantine_orphans(int, text, text, text, text, int) TO " + SVC_ROLE);
            su.createStatement().execute(
                "GRANT EXECUTE ON FUNCTION nexus.gc_restore_rereferenced(int, text, text, text) TO " + SVC_ROLE);
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

        tenantScope = new TenantScope(svcDs);
        catalogRepo = new CatalogRepository(tenantScope);
        var embedder = new ConstantEmbedder(1024);
        vectorRepo = new PgVectorRepository(tenantScope, embedder, embedder);
    }

    /** Minimal {@link Embedder}: every text embeds to the same fixed unit vector — content of the vector is irrelevant to this suite (only chash/collection/metadata/text movement is under test). */
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

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    /** Registers {@code docId} (physical_collection = ORIGIN_COL) and writes one manifest row for {@code chash}. */
    private void seedManifest(String tenant, String docId, String chash, String collection) {
        catalogRepo.upsertDocument(tenant, Map.of(
            "tumbler", docId,
            "title", "GC quarantine fixture " + docId,
            "content_type", "code",
            "corpus", "code",
            "physical_collection", collection
        ));
        catalogRepo.writeManifest(tenant, docId, collection, List.of(
            Map.<String, Object>of("position", 0, "chash", chash, "chunk_index", 0)
        ));
    }

    /**
     * RDR-191 Phase 5 (nexus-o8dil.29) finding: fk_catalog_chunks_chunk now
     * structurally blocks a real, pre-existing production race this file's
     * "re-reference before restore" tests deliberately construct — a manifest
     * write naming {@code originCollection} for a chash whose physical chunk
     * currently sits ONLY in {@code quarantineCollection} (gc_quarantine_orphans
     * already ran; gc_restore_rereferenced has not yet). Under the FK such a
     * write is now REJECTED at write time rather than silently landing (the
     * pre-existing behavior these tests exist to verify {@code
     * gc_restore_rereferenced}/{@code gc_expire_quarantine} handle correctly
     * once the row exists). This is flagged as a genuine follow-up finding (not
     * silently absorbed) — see the o8dil.29 completion report. Bypasses the FK
     * LOCALLY, the same drop/insert/re-add-NOT-VALID idiom used elsewhere in
     * this suite, so the SQL functions' own handling of an already-existing
     * re-referenced row stays covered.
     */
    private void seedManifestBypassingFk(String tenant, String docId, String chash, String collection)
            throws Exception {
        try (var conn = pg.createConnection("")) {
            conn.setAutoCommit(true);
            conn.createStatement().execute(
                "ALTER TABLE nexus.catalog_document_chunks DROP CONSTRAINT IF EXISTS fk_catalog_chunks_chunk");
        }
        seedManifest(tenant, docId, chash, collection);
        try (var conn = pg.createConnection("")) {
            conn.setAutoCommit(true);
            conn.createStatement().execute(
                "ALTER TABLE nexus.catalog_document_chunks "
                + "ADD CONSTRAINT fk_catalog_chunks_chunk "
                + "FOREIGN KEY (tenant_id, collection, chash) REFERENCES nexus.chunks (tenant_id, collection, chash) "
                + "ON UPDATE CASCADE DEFERRABLE INITIALLY IMMEDIATE NOT VALID");
        }
    }

    private void seedChunk(String tenant, String collection, String chash, String text, String title) {
        vectorRepo.upsertChunks(tenant, collection,
            List.of(chash), List.of(text), List.of(Map.of("title", title)));
    }

    private long chunkCount(String collection) throws SQLException {
        try (var conn = pg.createConnection(""); var st = conn.createStatement()) {
            var rs = st.executeQuery(
                "SELECT count(*) FROM " + DimTables.CHUNKS_TABLE_NAME + " WHERE collection = '" + collection + "' AND " + DimTables.embeddingColumn(1024) + " IS NOT NULL");
            rs.next();
            return rs.getLong(1);
        }
    }

    private String chunkText(String collection, String chash) throws SQLException {
        try (var conn = pg.createConnection(""); var st = conn.createStatement()) {
            var rs = st.executeQuery(
                "SELECT chunk_text FROM " + DimTables.CHUNKS_TABLE_NAME + " WHERE collection = '" + collection
                + "' AND chash = decode('" + chash + "', 'hex')");
            return rs.next() ? rs.getString(1) : null;
        }
    }

    private String metadataField(String collection, String chash, String key) throws SQLException {
        try (var conn = pg.createConnection(""); var st = conn.createStatement()) {
            var rs = st.executeQuery(
                "SELECT metadata->>'" + key + "' FROM " + DimTables.CHUNKS_TABLE_NAME + " WHERE collection = '" + collection
                + "' AND chash = decode('" + chash + "', 'hex')");
            return rs.next() ? rs.getString(1) : null;
        }
    }

    /** True when {@code nexus.catalog_collections} has a row for (tenant, name) — the
     *  nexus-syfes regression target: a zero-orphan/zero-restore GC pass must NOT
     *  leave one of these behind for a collection sibling that was never written to. */
    private boolean collectionRegistered(String tenant, String name) throws SQLException {
        try (var conn = pg.createConnection(""); var st = conn.createStatement()) {
            var rs = st.executeQuery(
                "SELECT 1 FROM nexus.catalog_collections WHERE tenant_id = '" + tenant
                + "' AND name = '" + name + "'");
            return rs.next();
        }
    }

    /** Row values from {@code nexus.catalog_collections} for (tenant, name); null fields
     *  if the row does not exist. Used to verify the split-on-"__" enrichment parity
     *  (content_type/owner_id/embedding_model/model_version) the SQL fix reproduces
     *  from {@code PgVectorRepository}'s (now-deleted) Java-side stub. */
    private record CollectionRow(String contentType, String ownerId, String embeddingModel, String modelVersion) {}

    private CollectionRow collectionRow(String tenant, String name) throws SQLException {
        try (var conn = pg.createConnection(""); var st = conn.createStatement()) {
            var rs = st.executeQuery(
                "SELECT content_type, owner_id, embedding_model, model_version FROM nexus.catalog_collections "
                + "WHERE tenant_id = '" + tenant + "' AND name = '" + name + "'");
            if (!rs.next()) return null;
            return new CollectionRow(rs.getString(1), rs.getString(2), rs.getString(3), rs.getString(4));
        }
    }

    // ── EQUIVALENCE: orphans present ─────────────────────────────────────────

    @Test
    void quarantineOrphans_movesUnreferencedChunk_leavesReferencedInPlace() throws Exception {
        String originCol = originCol("case1");
        String quarantineCol = quarantineCol("case1");
        String chashLive   = ch("gcq-live-1");
        String chashOrphan = ch("gcq-orphan-1");
        seedChunk(TENANT_A, originCol, chashLive, "live text", "Live Doc");
        seedChunk(TENANT_A, originCol, chashOrphan, "orphan text", "Orphan Doc");
        seedManifest(TENANT_A, "gcq.doc.live1", chashLive, originCol);
        // chashOrphan has NO manifest row — must be classified orphan.

        var outcome = vectorRepo.quarantineOrphans(TENANT_A, originCol, quarantineCol, "2026-08-10T00:00:00Z", 20);

        assertThat(outcome.moved()).isEqualTo(1L);
        assertThat(outcome.sample()).hasSize(1);
        assertThat(outcome.sample().get(0).get("chash")).isEqualTo(chashOrphan);
        assertThat(outcome.sample().get(0).get("title")).isEqualTo("Orphan Doc");

        // Zero wire cost is verified separately (Python-side call-spy), but the
        // SERVER-SIDE effect is verified here: origin loses exactly the orphan,
        // quarantine gains exactly it, stamped.
        assertThat(chunkText(originCol, chashLive)).isEqualTo("live text");
        assertThat(chunkText(originCol, chashOrphan)).isNull();
        assertThat(chunkText(quarantineCol, chashOrphan)).isEqualTo("orphan text");
        assertThat(metadataField(quarantineCol, chashOrphan, "origin_collection")).isEqualTo(originCol);
        assertThat(metadataField(quarantineCol, chashOrphan, "quarantined_at")).isEqualTo("2026-08-10T00:00:00Z");
    }

    // ── EQUIVALENCE: no orphans ───────────────────────────────────────────────

    @Test
    void quarantineOrphans_allReferenced_movesNothing() throws Exception {
        String originCol = originCol("case2");
        String quarantineCol = quarantineCol("case2");
        String chash = ch("gcq-live-2");
        seedChunk(TENANT_A, originCol, chash, "text", "Doc");
        seedManifest(TENANT_A, "gcq.doc.live2", chash, originCol);

        var outcome = vectorRepo.quarantineOrphans(TENANT_A, originCol, quarantineCol, "2026-08-10T00:00:00Z", 20);

        assertThat(outcome.moved()).isEqualTo(0L);
        assertThat(outcome.sample()).isEmpty();
        assertThat(chunkText(originCol, chash)).isEqualTo("text");
    }

    // ── STRUCTURAL: the pre-RDR-180 "undecidable identity" case cannot occur ──

    @Test
    void chash_isNotNullPrimaryKeyColumn_undecidableIdentityIsImpossible() {
        String originCol = originCol("case3");
        String quarantineCol = quarantineCol("case3");
        // Python's _prune_deleted_files refused to classify a chunk lacking
        // chunk_text_hash metadata ("unsafe_skipped"). Post-RDR-180 chash IS
        // the PK — there is no state in which a live chunks_<dim> row lacks
        // one, so that refusal branch is now structurally dead, not merely
        // untested.
        assertThatThrownBy(() -> {
            try (var conn = pg.createConnection(""); var st = conn.createStatement()) {
                st.execute(
                    "INSERT INTO " + DimTables.CHUNKS_TABLE_NAME + " (tenant_id, collection, chash, chunk_text, " + DimTables.embeddingColumn(1024) + ") "
                    + "VALUES ('" + TENANT_A + "', '" + originCol + "', NULL, 'x', "
                    + "('[' || repeat('0,', 1023) || '0]')::vector)");
            }
        }).isInstanceOf(SQLException.class);
    }

    // ── PK COLLISION on re-quarantine: upsert (last-write-wins), never an error ─

    @Test
    void quarantineOrphans_pkCollisionAtDestination_upsertsOverwritesStalePriorQuarantine() throws Exception {
        String originCol = originCol("case4");
        String quarantineCol = quarantineCol("case4");
        String chash = ch("gcq-collision-1");
        // Stale prior quarantine row already occupies the destination PK.
        seedChunk(TENANT_A, quarantineCol, chash, "STALE quarantined text", "Stale Title");
        // The live origin row (orphan, no manifest) with DIFFERENT text —
        // simulates content that changed then got orphaned again.
        seedChunk(TENANT_A, originCol, chash, "FRESH orphan text", "Fresh Title");

        var outcome = vectorRepo.quarantineOrphans(TENANT_A, originCol, quarantineCol, "2026-08-10T01:00:00Z", 20);

        assertThat(outcome.moved()).isEqualTo(1L);
        // Copy-then-delete + upsert semantics (matches Python's
        // _upsert_full -> col.upsert / upsert_chunks_with_embeddings):
        // the MOVING row wins, never a PK-violation error, never data loss.
        assertThat(chunkText(quarantineCol, chash)).isEqualTo("FRESH orphan text");
        assertThat(chunkText(originCol, chash)).isNull();
    }

    // ── restore: re-referenced chunk moves back ──────────────────────────────

    @Test
    void restoreRereferenced_movesBackWhenManifestReReferencesIt() throws Exception {
        String originCol = originCol("case5");
        String quarantineCol = quarantineCol("case5");
        String chash = ch("gcq-restore-1");
        seedChunk(TENANT_A, originCol, chash, "will be orphaned then healed", "Healed Doc");
        var q = vectorRepo.quarantineOrphans(TENANT_A, originCol, quarantineCol, "2026-08-10T02:00:00Z", 20);
        assertThat(q.moved()).isEqualTo(1L);

        // A heal re-references the chash for the origin collection.
        seedManifestBypassingFk(TENANT_A, "gcq.doc.heal1", chash, originCol);

        long restored = vectorRepo.restoreRereferenced(TENANT_A, quarantineCol, originCol);

        assertThat(restored).isEqualTo(1L);
        assertThat(chunkText(originCol, chash)).isEqualTo("will be orphaned then healed");
        assertThat(chunkText(quarantineCol, chash)).isNull();
        assertThat(metadataField(originCol, chash, "origin_collection"))
            .as("restore strips the quarantine stamps")
            .isNull();
    }

    // ── nexus-syfes: quarantine/origin sibling catalog_collections registration ─
    //
    // Regression coverage for the collections-drift defect: catalog-023's
    // ORIGINAL Java-side ensureCollectionRegistered() ran UNCONDITIONALLY,
    // before the SQL anti-join, in its own committed transaction — so a
    // zero-orphan (or zero-restore) pass over a clean collection still left
    // a permanently-registered, permanently-empty catalog_collections row
    // for the unused sibling. The fix (catalog-024) moves the registration
    // INSIDE gc_quarantine_orphans/gc_restore_rereferenced, guarded on
    // v_chashes actually being non-NULL.

    @Test
    void quarantineOrphans_zeroOrphans_leavesNoCatalogCollectionsRowForQuarantineSibling() throws Exception {
        String originCol = originCol("reg1");
        String quarantineCol = quarantineCol("reg1");
        String chash = ch("gcq-reg1-live");
        seedChunk(TENANT_A, originCol, chash, "text", "Doc");
        seedManifest(TENANT_A, "gcq.doc.reg1", chash, originCol);
        // quarantineCol has never been written to by anything.
        assertThat(collectionRegistered(TENANT_A, quarantineCol))
            .as("precondition: quarantine sibling never touched")
            .isFalse();

        var outcome = vectorRepo.quarantineOrphans(TENANT_A, originCol, quarantineCol, "2026-08-11T00:00:00Z", 20);

        assertThat(outcome.moved()).isEqualTo(0L);
        assertThat(collectionRegistered(TENANT_A, quarantineCol))
            .as("a zero-orphan pass must not register the unused quarantine sibling "
                + "(nexus-syfes: this is exactly the permanent-orphan row `nx doctor`'s "
                + "collections-drift check flagged)")
            .isFalse();
    }

    @Test
    void quarantineOrphans_nonZeroOrphans_stillRegistersQuarantineSibling_firstEverQuarantine() throws Exception {
        String originCol = originCol("reg2");
        String quarantineCol = quarantineCol("reg2");
        String chashOrphan = ch("gcq-reg2-orphan");
        seedChunk(TENANT_A, originCol, chashOrphan, "orphan text", "Orphan Doc");
        // No manifest row -> orphan. quarantineCol is untouched so far.
        assertThat(collectionRegistered(TENANT_A, quarantineCol))
            .as("precondition: this is the FIRST-EVER quarantine for this sibling — "
                + "the FK-satisfying row does not pre-exist")
            .isFalse();

        var outcome = vectorRepo.quarantineOrphans(TENANT_A, originCol, quarantineCol, "2026-08-11T00:00:00Z", 20);

        assertThat(outcome.moved()).isEqualTo(1L);
        assertThat(collectionRegistered(TENANT_A, quarantineCol))
            .as("a non-zero prune must still register the sibling — the FK path must keep working")
            .isTrue();
        var row = collectionRow(TENANT_A, quarantineCol);
        assertThat(row).isNotNull();
        // quarantineCol("reg2") = "quarantine-code__gcq-reg2__voyage-code-3__v1" — 4
        // segments, so the split-on-"__" enrichment applies (parity with the deleted
        // Java-side PgVectorRepository.ensureCollectionRegistered stub).
        assertThat(row.contentType()).isEqualTo("quarantine-code");
        assertThat(row.ownerId()).isEqualTo("gcq-reg2");
        assertThat(row.embeddingModel()).isEqualTo("voyage-code-3");
        assertThat(row.modelVersion()).isEqualTo("v1");
    }

    @Test
    void restoreRereferenced_zeroMatches_leavesNoCatalogCollectionsRowForOrigin() throws Exception {
        String originCol = originCol("reg3");
        String quarantineCol = quarantineCol("reg3");
        // Neither collection has ever been written to — restoreRereferenced finds
        // nothing in quarantineCol at all.
        assertThat(collectionRegistered(TENANT_A, originCol))
            .as("precondition: origin never touched")
            .isFalse();

        long restored = vectorRepo.restoreRereferenced(TENANT_A, quarantineCol, originCol);

        assertThat(restored).isEqualTo(0L);
        assertThat(collectionRegistered(TENANT_A, originCol))
            .as("a zero-match restore pass must not register an origin collection "
                + "that never received anything back from quarantine")
            .isFalse();
    }

    @Test
    void restoreRereferenced_nonZeroMatches_stillRegistersOrigin_firstEverRestore() throws Exception {
        String originCol = originCol("reg4");
        String quarantineCol = quarantineCol("reg4");
        String chash = ch("gcq-reg4-restore");
        // Seed the chunk directly into the QUARANTINE collection (no prior origin
        // write at all) with no origin_collection metadata stamp — gc_restore_
        // rereferenced's COALESCE(metadata->>'origin_collection', p_origin_collection)
        // defaults an unstamped row to "belongs to whatever origin is asked for".
        seedChunk(TENANT_A, quarantineCol, chash, "quarantined text", "Quarantined Doc");
        // Manifest references chash for originCol — originCol itself is NEVER
        // written to by seedChunk/upsertChunks, so its catalog_collections row
        // does not pre-exist: this is the first-ever-restore case.
        seedManifestBypassingFk(TENANT_A, "gcq.doc.reg4", chash, originCol);
        assertThat(collectionRegistered(TENANT_A, originCol))
            .as("precondition: this is the FIRST-EVER restore into this origin — "
                + "the FK-satisfying row does not pre-exist")
            .isFalse();

        long restored = vectorRepo.restoreRereferenced(TENANT_A, quarantineCol, originCol);

        assertThat(restored).isEqualTo(1L);
        assertThat(collectionRegistered(TENANT_A, originCol))
            .as("a non-zero restore must still register the origin — the FK path must keep working")
            .isTrue();
        var row = collectionRow(TENANT_A, originCol);
        assertThat(row).isNotNull();
        // originCol("reg4") = "code__gcq-reg4__voyage-code-3__v1" — 4 segments.
        assertThat(row.contentType()).isEqualTo("code");
        assertThat(row.ownerId()).isEqualTo("gcq-reg4");
        assertThat(row.embeddingModel()).isEqualTo("voyage-code-3");
        assertThat(row.modelVersion()).isEqualTo("v1");
    }

    // ── expire: grace-window floor refuses a mass hard-delete, force overrides ─

    @Test
    void expireQuarantine_floorRefusesMassDelete_thenForceOverrides() throws Exception {
        String originCol = originCol("case6");
        String quarantineCol = quarantineCol("case6");
        // Seed 10 quarantined rows, all past the cutoff (old timestamp),
        // "mine" for originCol — enough to trip a permissive floor.
        for (int i = 0; i < 10; i++) {
            String chash = ch("gcq-expire-" + i);
            seedChunk(TENANT_A, originCol, chash, "expiring text " + i, "Expiring " + i);
        }
        var q = vectorRepo.quarantineOrphans(TENANT_A, originCol, quarantineCol, "2026-07-01T00:00:00Z", 20);
        assertThat(q.moved()).isEqualTo(10L);

        var refused = vectorRepo.expireQuarantine(
            TENANT_A, quarantineCol, originCol,
            "2026-08-01T00:00:00Z", /* cutoff, after quarantined_at */
            0.5, /* floor_fraction */ 5, /* floor_min_chunks */ false /* force */);
        assertThat(refused.expired()).isEqualTo(0L);
        assertThat(refused.refused()).isEqualTo(10L);
        assertThat(chunkCount(quarantineCol)).isGreaterThanOrEqualTo(10L);

        var forced = vectorRepo.expireQuarantine(
            TENANT_A, quarantineCol, originCol,
            "2026-08-01T00:00:00Z", 0.5, 5, true /* force */);
        assertThat(forced.expired()).isEqualTo(10L);
        assertThat(forced.refused()).isEqualTo(0L);
    }

    @Test
    void expireQuarantine_underFloor_deletesWithoutForce() throws Exception {
        String originCol = originCol("case7");
        String quarantineCol = quarantineCol("case7");
        String chash = ch("gcq-expire-small-1");
        seedChunk(TENANT_A, originCol, chash, "small batch expiring", "Small");
        var q = vectorRepo.quarantineOrphans(TENANT_A, originCol, quarantineCol, "2026-07-01T00:00:00Z", 20);
        assertThat(q.moved()).isEqualTo(1L);

        // floor_min_chunks=50: this one-row batch never trips the floor.
        var outcome = vectorRepo.expireQuarantine(
            TENANT_A, quarantineCol, originCol,
            "2026-08-01T00:00:00Z", 0.5, 50, false);

        assertThat(outcome.expired()).isEqualTo(1L);
        assertThat(outcome.refused()).isEqualTo(0L);
        assertThat(chunkText(quarantineCol, chash)).isNull();
    }

    // ── nexus-wnpet: manifest-referenced chunks are never hard-deleted ───────
    //
    // gc_expire_quarantine previously never read catalog_document_chunks at
    // all -- a past-cutoff quarantined chash still named by a manifest row
    // (e.g. gc_restore_rereferenced has not yet run, or a heal re-referenced
    // it after quarantine) was permanently destroyed unconditionally. The
    // fix mirrors gc_restore_rereferenced's own rescue predicate as a guard.

    @Test
    void expireQuarantine_manifestReferencedChunk_isRefusedNotExpired_survivesEvenWithForce() throws Exception {
        String originCol = originCol("case9");
        String quarantineCol = quarantineCol("case9");
        String chash = ch("gcq-manifest-guard-1");

        seedChunk(TENANT_A, originCol, chash, "still needed text", "Still Needed");
        var q = vectorRepo.quarantineOrphans(TENANT_A, originCol, quarantineCol, "2026-07-01T00:00:00Z", 20);
        assertThat(q.moved()).isEqualTo(1L);

        // A manifest row re-references the origin collection for this chash AFTER
        // quarantine -- simulating gc_restore_rereferenced not having run yet, or a
        // heal landing between quarantine and expire. This is the ordering-violation
        // case the guard exists for.
        seedManifestBypassingFk(TENANT_A, "gcq.doc.guard9", chash, originCol);

        var outcome = vectorRepo.expireQuarantine(
            TENANT_A, quarantineCol, originCol,
            "2026-08-01T00:00:00Z", /* cutoff, after quarantined_at */
            0.5, /* floor_fraction */ 50, /* floor_min_chunks -- never trips at n=1 */ false /* force */);

        assertThat(outcome.expired())
            .as("a manifest-referenced chash must never be counted as expired")
            .isEqualTo(0L);
        assertThat(outcome.refused())
            .as("a manifest-referenced chash is refused, same population class as a floor refusal")
            .isEqualTo(1L);
        assertThat(chunkText(quarantineCol, chash))
            .as("the chunk must physically survive")
            .isEqualTo("still needed text");

        // force=true overrides the population-size safety heuristic (the floor), never
        // the manifest-reference correctness guarantee -- a still-referenced chunk must
        // stay protected regardless.
        var forced = vectorRepo.expireQuarantine(
            TENANT_A, quarantineCol, originCol,
            "2026-08-01T00:00:00Z", 0.5, 50, true /* force */);

        assertThat(forced.expired())
            .as("force must not free a manifest-referenced chash")
            .isEqualTo(0L);
        assertThat(forced.refused()).isEqualTo(1L);
        assertThat(chunkText(quarantineCol, chash))
            .as("the chunk must still survive even under force=true")
            .isEqualTo("still needed text");
    }

    @Test
    void expireQuarantine_mixedBatch_expiresUnreferenced_refusesManifestReferenced() throws Exception {
        String originCol = originCol("case10");
        String quarantineCol = quarantineCol("case10");
        String chashFree = ch("gcq-manifest-guard-free");
        String chashHeld = ch("gcq-manifest-guard-held");

        seedChunk(TENANT_A, originCol, chashFree, "free text", "Free");
        seedChunk(TENANT_A, originCol, chashHeld, "held text", "Held");
        var q = vectorRepo.quarantineOrphans(TENANT_A, originCol, quarantineCol, "2026-07-01T00:00:00Z", 20);
        assertThat(q.moved()).isEqualTo(2L);

        // Only chashHeld gets re-referenced; chashFree stays genuinely orphaned.
        seedManifestBypassingFk(TENANT_A, "gcq.doc.guard10", chashHeld, originCol);

        var outcome = vectorRepo.expireQuarantine(
            TENANT_A, quarantineCol, originCol,
            "2026-08-01T00:00:00Z", 0.9, /* floor_fraction high -- must not trip at 1/2 */
            50, false);

        assertThat(outcome.expired())
            .as("the genuinely unreferenced chash still expires exactly as before")
            .isEqualTo(1L);
        assertThat(outcome.refused())
            .as("the manifest-referenced chash is refused, not silently dropped from the count")
            .isEqualTo(1L);
        assertThat(chunkText(quarantineCol, chashFree)).isNull();
        assertThat(chunkText(quarantineCol, chashHeld)).isEqualTo("held text");
    }

    // ── tenant isolation (RLS, SECURITY INVOKER) ─────────────────────────────

    @Test
    void quarantineOrphans_crossTenant_movesNothing() throws Exception {
        String originCol = originCol("case8");
        String quarantineCol = quarantineCol("case8");
        String chash = ch("gcq-iso-1");
        seedChunk(TENANT_A, originCol, chash, "tenant A only", "A Doc");
        // No manifest row -> would be an orphan for TENANT_A, but TENANT_B's
        // GUC scope must see zero rows of TENANT_A's data (RLS).

        var outcome = vectorRepo.quarantineOrphans(TENANT_B, originCol, quarantineCol, "2026-08-10T00:00:00Z", 20);

        assertThat(outcome.moved()).isEqualTo(0L);
        assertThat(chunkText(originCol, chash))
            .as("tenant A's row survives a tenant B GC pass untouched")
            .isEqualTo("tenant A only");
    }

    // ── nexus-sa731: definition pin — sweep gate + inline-guard collapse ────
    //
    // catalog-024's gc_quarantine_orphans evaluated its manifest-reference
    // guard SELECT and its DELETE/move as two separate READ COMMITTED
    // statements with no lock on the path -- a manifest write committing
    // between the two could get quarantined out from under it (write-skew).
    // catalog-028 fixes this by taking the SAME per-(tenant, collection)
    // advisory sweep gate CatalogRepository#runSweepTransaction uses, plus
    // (belt and braces) folding the guard inline into each move statement's
    // own WHERE instead of a precomputed chash array.
    //
    // This is a DEFINITION PIN, not a concurrency reproduction -- a
    // deterministic race test is exactly what this structural fix exists to
    // avoid needing (see the catalog-028 changeset header). It asserts the
    // LIVE function body, via pg_get_functiondef, rather than trying to
    // provoke the race under test. Kill-controlled: temporarily removing the
    // lock line from the catalog-028 changeset was confirmed to turn this
    // test RED (see T2 nexus-sa731-fix-gc-quarantine-lock-2026-08-13 for the
    // recorded run), then the line was restored and the suite re-verified
    // GREEN.
    //
    // ROUND 2 (stacked review, T2 nexus/review-sa731-catalog028-2026-08-13
    // [22403] Important finding + nexus/critique-sa731-catalog028-2026-08-13
    // [22404] Significant finding): the initial fix's EXCLUSIVE acquisition
    // had no lock_timeout/statement_timeout, unlike the
    // acquireSweepGateExclusive model it claims to mirror -- an unbounded
    // wait here would hang the synchronous HTTP handler
    // (VectorHandler#handleGcQuarantineOrphans) indefinitely behind a
    // long-running manifest writer holding the SHARED half. Extended below
    // to also pin the timeout pair and the lock_not_available handler,
    // kill-controlled the same way (removing the set_config lines turns the
    // new assertions RED; see T2 nexus-sa731-fix-gc-quarantine-lock-2026-08-13
    // round-2 section for the recorded run).

    @Test
    void gcQuarantineOrphans_definitionPin_hasSweepGateLock_noStandaloneGuardSelect() throws Exception {
        String functionDef = functionDefinition(
            "nexus.gc_quarantine_orphans(int, text, text, text, text, int)");

        assertThat(functionDef)
            .as("must take the EXCLUSIVE half of the same advisory sweep-gate key "
                + "CatalogRepository#acquireSweepGateExclusive uses, closing the "
                + "manifest-insert-vs-quarantine-move write-skew window")
            .contains("pg_advisory_xact_lock(hashtext('sweepgate:'")
            .doesNotContain("pg_advisory_xact_lock_shared");

        assertThat(functionDef)
            .as("the old precomputed-array guard (SELECT array_agg(...) INTO v_chashes, "
                + "then chash = ANY(v_chashes)) must be gone -- the guard now lives inline "
                + "in each move statement's own WHERE, re-evaluated at that statement's own "
                + "snapshot, not trusted from an array computed earlier in the transaction")
            .doesNotContain("v_chashes")
            .doesNotContain("= ANY(v_chashes)");

        // RDR-191 repoint batch (vectors-005-repoint-functions-views.xml,
        // nexus-o8dil.18) re-created this function dim-agnostic (the row's
        // populated embedding_<dim> column carries straight through a move
        // regardless of dim -- see that changeset's header, "DIM DOES NOT
        // MATTER" bucket) AND added an N7-style cross-dim collision guard
        // (Critical-1 fix, T2 22454) that legitimately introduces its OWN
        // array_agg, unrelated to the old registration guard's precomputed
        // membership array: it builds v_collision_sample, a capped sample of
        // colliding chashes for the RAISE EXCEPTION message, not a `chash =
        // ANY(...)` guard feeding a move statement. A blanket "no array_agg
        // anywhere" ban is therefore no longer the correct pin -- re-derived
        // below to still assert the OLD guard shape is gone (containsOnlyOnce
        // + tied to the collision-sample INTO target) without going vacuous.
        assertThat(functionDef)
            .as("array_agg's ONLY legitimate use in this function is building the "
                + "N7-style cross-dim collision sample (v_collision_sample) for the "
                + "Critical-1 guard (T2 22454) -- the OLD registration guard's "
                + "precomputed membership array is already pinned absent above "
                + "(v_chashes / = ANY(v_chashes)); this pins that array_agg has not "
                + "crept back in as a SECOND, unrelated precomputed-array guard")
            .containsOnlyOnce("array_agg")
            .contains("INTO v_collision_count, v_collision_sample");

        // "NOT EXISTS" occurrences: the RDR-191 repoint made this function
        // dim-agnostic (no more x3 per-dim branching), so the count collapsed
        // from the old dim-branched shape's 12 down to FIVE, each an
        // INDEPENDENT re-evaluation of the manifest-reference predicate (or,
        // for the pre-flight's outer check, of "is there anything to move at
        // all") -- none of them handing a precomputed chash array to another:
        // (1) the pre-flight's own outer `IF NOT EXISTS (...)`, (2) the
        // pre-flight's inner manifest-reference guard, (3) the orphan CTE that
        // feeds the collision-detection query, (4) the copy-to-quarantine
        // INSERT's own inline guard, (5) the remove-from-origin DELETE's own
        // inline guard. This count is still the load-bearing signal that the
        // guard lives inline in each statement rather than via a standalone
        // array-building guard SELECT (sa731 semantics preserved across the
        // dim-collapse).
        int notExistsCount = functionDef.split("NOT EXISTS", -1).length - 1;
        assertThat(notExistsCount)
            .as("inline/pre-flight NOT EXISTS occurrences must be FIVE -- pre-flight's "
                + "outer + inner, the collision-detection orphan CTE, the copy INSERT, "
                + "and the DELETE -- now that the RDR-191 repoint made this function "
                + "dim-agnostic (no more x3 per-dim multiplication), not 3x total via a "
                + "standalone array-building guard SELECT")
            .isEqualTo(5);
    }

    // ── nexus-sa731 ROUND 2: definition pin — bounded-wait timeout mirror ───
    //
    // Stacked review (T2 nexus/review-sa731-catalog028-2026-08-13 [22403]
    // Important finding + nexus/critique-sa731-catalog028-2026-08-13 [22404]
    // Significant finding): the round-1 fix's EXCLUSIVE acquisition
    // (PERFORM pg_advisory_xact_lock(...), asserted by the pin test above)
    // had NO lock_timeout/statement_timeout, unlike the
    // acquireSweepGateExclusive model the changeset header claims to
    // mirror (CatalogRepository.java:3898-3907). This function is invoked
    // synchronously from VectorHandler#handleGcQuarantineOrphans, an HTTP
    // handler with no request-level timeout of its own -- an unbounded wait
    // behind a long-running manifest writer holding the SHARED half would
    // hang that thread indefinitely, the exact failure direction the sweep
    // gate's own design exists to avoid.
    //
    // Separate test (not folded into the round-1 pin above) so a break in
    // either invariant is unambiguous from the failing test name alone.
    // Kill-controlled the same way as the round-1 pin: temporarily removing
    // the two `set_config` lines from the catalog-028 changeset was
    // confirmed to turn this test RED, then the lines were restored and the
    // suite re-verified GREEN (see T2 nexus-sa731-fix-gc-quarantine-lock-
    // 2026-08-13's round-2 section for the recorded run).

    @Test
    void gcQuarantineOrphans_definitionPin_boundsAcquisitionWithSweepGateTimeouts() throws Exception {
        String functionDef = functionDefinition(
            "nexus.gc_quarantine_orphans(int, text, text, text, text, int)");

        assertThat(functionDef)
            .as("must bound the EXCLUSIVE acquisition with the same "
                + "lock_timeout/statement_timeout values acquireSweepGateExclusive "
                + "sets (2000ms/5000ms), and re-raise a timed-out acquisition as an "
                + "explicit, retryable, gate-naming error rather than blocking forever")
            .contains("set_config('lock_timeout', '2000', true)")
            .contains("set_config('statement_timeout', '5000', true)")
            .contains("WHEN lock_not_available THEN")
            .contains("ERRCODE = 'lock_not_available'");
    }

    /** {@code pg_get_functiondef} for {@code signature} (schema-qualified, e.g.
     *  {@code "nexus.fn(int, text)"}) -- a definition pin needs no RLS/role
     *  concern (reading a function's own source, not tenant data), so the plain
     *  superuser connection used by the other raw-SQL fixture helpers above
     *  suffices. */
    private String functionDefinition(String signature) throws SQLException {
        try (var conn = pg.createConnection(""); var st = conn.createStatement()) {
            var rs = st.executeQuery(
                "SELECT pg_get_functiondef('" + signature + "'::regprocedure)");
            rs.next();
            return rs.getString(1);
        }
    }
}
