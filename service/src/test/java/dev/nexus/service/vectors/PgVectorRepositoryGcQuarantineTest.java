// SPDX-License-Identifier: AGPL-3.0-or-later
package dev.nexus.service.vectors;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.PgContainerHelper;
import dev.nexus.service.db.CatalogRepository;
import dev.nexus.service.db.Chash;
import dev.nexus.service.db.TenantScope;
import liquibase.Contexts;
import liquibase.Liquibase;
import liquibase.database.DatabaseFactory;
import liquibase.database.jvm.JdbcConnection;
import liquibase.resource.ClassLoaderResourceAccessor;
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
            su.setAutoCommit(true);
            su.createStatement().execute(
                "DO $$ BEGIN " +
                "  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '" + SVC_ROLE + "') THEN " +
                "    CREATE ROLE " + SVC_ROLE + " LOGIN PASSWORD '" + SVC_PASS + "' NOSUPERUSER NOBYPASSRLS; " +
                "  END IF; " +
                "END $$");
            su.createStatement().execute(
                "DO $$ BEGIN " +
                "  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nexus_svc') THEN " +
                "    CREATE ROLE nexus_svc LOGIN PASSWORD 'nexus_svc_pass'; " +
                "  END IF; " +
                "END $$");
        }

        try (Connection su = pg.createConnection("")) {
            var lb = new Liquibase(
                "db/changelog/db.changelog-master.xml",
                new ClassLoaderResourceAccessor(),
                DatabaseFactory.getInstance().findCorrectDatabaseImplementation(
                    new JdbcConnection(su)));
            lb.update(new Contexts());
        }

        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            su.createStatement().execute("GRANT USAGE ON SCHEMA nexus TO " + SVC_ROLE);
            su.createStatement().execute("GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA nexus TO " + SVC_ROLE);
            su.createStatement().execute("GRANT USAGE ON SCHEMA nexus TO " + SVC_ROLE);
            su.createStatement().execute("GRANT USAGE ON SEQUENCE nexus.catalog_links_id_seq TO " + SVC_ROLE);
            su.createStatement().execute(
                "GRANT EXECUTE ON FUNCTION nexus.gc_quarantine_orphans(int, text, text, text, text, int) TO " + SVC_ROLE);
            su.createStatement().execute(
                "GRANT EXECUTE ON FUNCTION nexus.gc_restore_rereferenced(int, text, text, text) TO " + SVC_ROLE);
            su.createStatement().execute(
                "GRANT EXECUTE ON FUNCTION nexus.gc_expire_quarantine(int, text, text, text, text, float8, int, boolean) TO " + SVC_ROLE);
            su.createStatement().execute(
                "ALTER ROLE " + SVC_ROLE + " SET search_path TO nexus, public");
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

    private void seedChunk(String tenant, String collection, String chash, String text, String title) {
        vectorRepo.upsertChunks(tenant, collection,
            List.of(chash), List.of(text), List.of(Map.of("title", title)));
    }

    private long chunkCount(String collection) throws SQLException {
        try (var conn = pg.createConnection(""); var st = conn.createStatement()) {
            var rs = st.executeQuery(
                "SELECT count(*) FROM nexus.chunks_1024 WHERE collection = '" + collection + "'");
            rs.next();
            return rs.getLong(1);
        }
    }

    private String chunkText(String collection, String chash) throws SQLException {
        try (var conn = pg.createConnection(""); var st = conn.createStatement()) {
            var rs = st.executeQuery(
                "SELECT chunk_text FROM nexus.chunks_1024 WHERE collection = '" + collection
                + "' AND chash = decode('" + chash + "', 'hex')");
            return rs.next() ? rs.getString(1) : null;
        }
    }

    private String metadataField(String collection, String chash, String key) throws SQLException {
        try (var conn = pg.createConnection(""); var st = conn.createStatement()) {
            var rs = st.executeQuery(
                "SELECT metadata->>'" + key + "' FROM nexus.chunks_1024 WHERE collection = '" + collection
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
                    "INSERT INTO nexus.chunks_1024 (tenant_id, collection, chash, chunk_text, embedding) "
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
        seedManifest(TENANT_A, "gcq.doc.heal1", chash, originCol);

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
        seedManifest(TENANT_A, "gcq.doc.reg4", chash, originCol);
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
        seedManifest(TENANT_A, "gcq.doc.guard9", chash, originCol);

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
        seedManifest(TENANT_A, "gcq.doc.guard10", chashHeld, originCol);

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
}
