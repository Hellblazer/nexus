package dev.nexus.service;

import dev.nexus.service.db.StagingPromoteOps;
import dev.nexus.service.db.StagingPromoteOps.PromotePreconditionException;
import dev.nexus.service.db.TenantScope;
import liquibase.Contexts;
import liquibase.Liquibase;
import liquibase.database.DatabaseFactory;
import liquibase.database.jvm.JdbcConnection;
import liquibase.resource.ClassLoaderResourceAccessor;
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
import java.sql.ResultSet;
import java.sql.SQLException;
import java.util.HexFormat;
import java.util.List;
import java.util.Map;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.TimeoutException;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * RDR-180 LAND-THEN-TRANSFORM promote (nexus-jxizy.10.3) — integration.
 *
 * <p>The reconciliation's critical scenarios, each as a live PG test:
 * C2 (finalize is idempotent + re-runnable; a LATE collection's pointers
 * promote on the next finalize), M1 (collapse pair promotes deterministically
 * to ONE row), H1 (staged dim disagreeing with the name-implied dim refuses),
 * R5 (promote into a populated target converges; re-promote adds nothing).
 *
 * <p>nexus-lgdel.l1: C1 (cross-collection alias contradiction) and C4 (a
 * reference-only row resolving through a cross-collection alias) were
 * scenarios of the {@code nexus.chash_alias} legacy-reference resolution
 * mechanism, RETIRED along with the table — content promotion is now
 * purely digest-keyed and manifest/pointer-store promotion is direct-
 * 64-hex-only, so neither scenario can occur any more. See the deleted
 * Order(3)/(6)/(12)/(14)/(24) tests' retirement comments in this file for
 * what they used to cover.
 *
 * <p>Staging accepts every legacy width VERBATIM — no constraint-drop
 * seeding dance (the land-then-transform win the in-store suite needs).
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
@TestMethodOrder(MethodOrderer.OrderAnnotation.class)
class StagingPromoteOpsIntegrationTest {

    private static final String SVC_ROLE = "svc_promote_test";
    private static final String SVC_PASS = "svc_promote_pw";
    private static final String T1 = "t-promote-a";
    // nexus-o8dil.50: dedicated tenant for the orphan-synthesize dim-
    // coverage tests, RLS-isolated from T1's own multi-order state so
    // orphan/alias counts assert exact values instead of deltas against
    // whatever earlier @Order tests left behind (several leave staged
    // "dropped" orphan rows in place permanently -- drop never deletes,
    // only counts -- which would otherwise get swept up the first time
    // ANY test on the same tenant calls finalizeTenant(tenant, true)).
    private static final String T_DIM = "t-promote-dim";
    // RDR-194 D0.9 (nexus-tk070.p3a): dedicated tenant for the
    // non-conformant topic_assignments.doc_id reject test, isolated from
    // T1's ordered sequence for the same reason T_DIM is (a fresh tenant
    // means an exact assertion instead of a delta against whatever earlier
    // @Order tests left staged).
    private static final String T_REJECT = "t-promote-reject";

    private static final String COLL_A = "knowledge__ka__bge-base-en-v15-768__v1";
    private static final String COLL_B = "knowledge__kb__bge-base-en-v15-768__v1";
    private static final String COLL_LATE = "knowledge__late__bge-base-en-v15-768__v1";

    private static final String TEXT_1 = "promote content alpha";
    private static final String TEXT_2 = "promote content bravo";
    private static final String TEXT_DUP = "promote duplicated text";

    PostgreSQLContainer<?> pg;
    com.zaxxer.hikari.HikariDataSource svcDs;
    TenantScope scope;
    StagingPromoteOps ops;

    private static String hex(byte[] b) {
        return HexFormat.of().formatHex(b);
    }

    private static String digestHex(String text) {
        try {
            return hex(MessageDigest.getInstance("SHA-256")
                .digest(text.getBytes(StandardCharsets.UTF_8)));
        } catch (Exception e) {
            throw new IllegalStateException(e);
        }
    }

    /** The RDR-108-era 32-hex legacy id for *text*. */
    private static String legacy32(String text) {
        return digestHex(text).substring(0, 32);
    }

    private static String vec(int dim) {
        StringBuilder sb = new StringBuilder("[");
        for (int i = 0; i < dim; i++) {
            if (i > 0) sb.append(',');
            sb.append('0');
        }
        return sb.append(']').toString();
    }

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            for (String role : new String[] {SVC_ROLE, "nexus_svc"}) {
                su.createStatement().execute(
                    "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '"
                    + role + "') THEN CREATE ROLE " + role + " LOGIN PASSWORD '"
                    + (role.equals(SVC_ROLE) ? SVC_PASS : "nexus_svc_pass")
                    + "'; END IF; END $$");
            }
        }
        try (Connection su = pg.createConnection("")) {
            new Liquibase("db/changelog/db.changelog-master.xml",
                new ClassLoaderResourceAccessor(),
                DatabaseFactory.getInstance().findCorrectDatabaseImplementation(
                    new JdbcConnection(su)))
                .update(new Contexts());
        }
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            su.createStatement().execute("GRANT USAGE ON SCHEMA nexus TO " + SVC_ROLE);
            su.createStatement().execute("GRANT USAGE ON SCHEMA staging TO " + SVC_ROLE);
            su.createStatement().execute(
                "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA nexus TO " + SVC_ROLE);
            su.createStatement().execute(
                "GRANT SELECT, INSERT, UPDATE, DELETE, TRUNCATE ON ALL TABLES IN SCHEMA staging TO " + SVC_ROLE);
            su.createStatement().execute(
                "GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA nexus TO " + SVC_ROLE);
            su.createStatement().execute(
                "ALTER ROLE " + SVC_ROLE + " SET search_path TO nexus, public");
        }
        var config = new com.zaxxer.hikari.HikariConfig();
        config.setJdbcUrl(pg.getJdbcUrl());
        config.setUsername(SVC_ROLE);
        config.setPassword(SVC_PASS);
        config.setMaximumPoolSize(3);
        config.setAutoCommit(true);
        svcDs = new com.zaxxer.hikari.HikariDataSource(config);
        scope = new TenantScope(svcDs);
        ops = new StagingPromoteOps(scope);
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    private void landChunk(String coll, int dim, String ref, String text, String vecLit) {
        scope.withTenant(T1, ctx -> {
            ctx.execute("INSERT INTO staging.chunks "
                + "(tenant_id, collection, dim, legacy_ref, chunk_text, embedding, model) "
                + "VALUES (?, ?, ?, ?, ?, " + (vecLit == null ? "NULL" : "'" + vecLit + "'::vector") + ", 'bge-768') "
                + "ON CONFLICT (tenant_id, collection, legacy_ref) DO UPDATE SET chunk_text = excluded.chunk_text",
                T1, coll, dim, ref, text);
            return null;
        });
    }

    private int count(String sql) {
        return scope.withTenant(T1, ctx -> ctx.fetchOne(sql).get(0, Integer.class));
    }

    // nexus-o8dil.50: count() above is hardcoded to T1 -- every nexus.* table
    // is FORCE ROW LEVEL SECURITY on current_setting('nexus.tenant'), so a
    // query run under T1's session context returns ZERO rows for T_DIM data
    // regardless of an explicit `tenant_id = ...` predicate in the SQL text
    // (RLS filters before the WHERE clause is evaluated against visible
    // rows). The dim-coverage tests below run under T_DIM and need this
    // tenant-parameterized twin.
    private int countAs(String tenant, String sql) {
        return scope.withTenant(tenant, ctx -> ctx.fetchOne(sql).get(0, Integer.class));
    }

    // ── Order 1: the full happy path, all three legacy widths ────────────────

    @Test
    @Order(1)
    void promote_allWidths_landAtDigests() {
        // nexus-lgdel.l1: legacy_ref is now PURELY an M1 deterministic-
        // tiebreak input (staged content collapsing to the same digest
        // picks the row whose legacy_ref already equals the digest hex,
        // else the lexicographically-min ref) — it is no longer an
        // identity that gets aliased. Content promotion is digest-keyed
        // regardless of legacy_ref's own shape, so all three widths land
        // identically; the former per-width "alias_rows" count and
        // nexus.chash_alias assertions are RETIRED with the table.
        String legacy16 = "b46c7915c303245f";                       // pre-RDR-108 shape
        String legacy32 = legacy32(TEXT_1);                          // RDR-108-era shape
        String canonical = digestHex(TEXT_2);                        // already canonical
        landChunk(COLL_A, 768, legacy16, "sixteen char content", vec(768));
        landChunk(COLL_A, 768, legacy32, TEXT_1, vec(768));
        landChunk(COLL_A, 768, canonical, TEXT_2, vec(768));

        Map<String, Object> counts = ops.promoteCollection(T1, COLL_A, 768);
        assertThat(counts.get("promoted")).isEqualTo(3);

        assertThat(count("SELECT count(*) FROM nexus.chunks "
            + "WHERE collection = '" + COLL_A + "' AND octet_length(chash) = 32"))
            .isEqualTo(3);
        assertThat(count("SELECT count(*) FROM nexus.chunks "
            + "WHERE collection = '" + COLL_A + "' AND encode(chash,'hex') = '"
            + digestHex("sixteen char content") + "'")).isEqualTo(1);
        assertThat(count("SELECT count(*) FROM nexus.chunks "
            + "WHERE collection = '" + COLL_A + "' AND encode(chash,'hex') = '"
            + digestHex(TEXT_1) + "'")).isEqualTo(1);
        // RDR-086 metadata parity (--guided gate run 3 catch, nexus-
        // jxizy.10.10): serving-path writes stamp chunk_text_hash into
        // metadata client-side; the citation resolver's final hop
        // (/v1/vectors/get where={"chunk_text_hash": ...}) filters on it.
        // Promoted rows must be indistinguishable from serving-path writes,
        // so promote stamps the digest hex at INSERT — a verbatim
        // chunk_meta copy leaves every migrated chunk invisible to
        // citations.
        assertThat(count("SELECT count(*) FROM nexus.chunks "
            + "WHERE collection = '" + COLL_A + "' "
            + "AND metadata->>'chunk_text_hash' IS DISTINCT FROM encode(chash,'hex')"))
            .as("every promoted row's metadata chunk_text_hash mirrors its chash")
            .isEqualTo(0);
    }

    // ── Order 2: collapse pair (M1) — one row, both refs aliased ─────────────

    @Test
    @Order(2)
    void promote_collapsePair_oneRowDeterministicKeeper() {
        // nexus-lgdel.l1: the "both collapse-pair refs alias to the shared
        // digest" assertion is RETIRED with nexus.chash_alias — neither ref
        // is aliased any more (see Order 1's identical note). The surviving
        // capability this test proves is the M1 collapse itself: two staged
        // rows with identical content land as ONE content row.
        String refX = "aaaa0000aaaa0000aaaa0000aaaa0000";
        String refY = "bbbb1111bbbb1111bbbb1111bbbb1111";
        landChunk(COLL_A, 768, refX, TEXT_DUP, vec(768));
        landChunk(COLL_A, 768, refY, TEXT_DUP, vec(768));

        ops.promoteCollection(T1, COLL_A, 768);
        assertThat(count("SELECT count(*) FROM nexus.chunks "
            + "WHERE encode(chash,'hex') = '" + digestHex(TEXT_DUP) + "'"))
            .as("identical text collapses to ONE content row").isEqualTo(1);
    }

    // nexus-lgdel.l1: Order 3 (promote_sameRefDifferentContentAcrossCollections_
    // failsLoud) DELETED — its subject, the C1 committed-alias-contradiction
    // guard, is retired: without a committed alias map to check a staged ref
    // against, two collections landing the "same" legacy ref with different
    // content simply promote each collection's own content independently
    // (each keyed by its OWN digest) rather than conflicting.

    // ── Order 4: H1 — dim disagreement refuses ───────────────────────────────

    @Test
    @Order(4)
    void promote_dimMismatch_refuses() {
        landChunk(COLL_B, 384, "cccc2222cccc2222cccc2222cccc2222", "wrong dim content", vec(384));
        assertThatThrownBy(() -> ops.promoteCollection(T1, COLL_B, 768))
            .isInstanceOf(PromotePreconditionException.class)
            .hasMessageContaining("dim");
        scope.withTenant(T1, ctx -> {
            ctx.execute("DELETE FROM staging.chunks WHERE collection = ?", COLL_B);
            return null;
        });
    }

    // ── Order 5: NULL embedding refuses (embed-fill precedes promote) ────────

    @Test
    @Order(5)
    void promote_nullEmbedding_refuses() {
        landChunk(COLL_B, 768, "dddd3333dddd3333dddd3333dddd3333", "no vector content", null);
        assertThatThrownBy(() -> ops.promoteCollection(T1, COLL_B, 768))
            .isInstanceOf(PromotePreconditionException.class)
            .hasMessageContaining("embedding");
        scope.withTenant(T1, ctx -> {
            ctx.execute("DELETE FROM staging.chunks WHERE collection = ?", COLL_B);
            return null;
        });
    }

    // nexus-lgdel.l1: Order 6 (finalize_promotesPointers_resolvesCrossCollection
    // Reference) DELETED — its subject was legacy32-shaped manifest/topic_
    // assignments/frecency/relevance_log pointers resolving to their
    // canonical digest through nexus.chash_alias. That resolution route is
    // retired with the table: manifestResolvable and the frecency/
    // relevance_log promote predicates are now direct-64-hex-only, and
    // finalizeTenant's non-conformant topic_assignments.doc_id reject
    // (Order 34) now THROWS on a staged legacy32-shaped doc_id rather than
    // leaving it silently staged — this scenario cannot run to completion
    // under the new contract at all.

    // ── Order 7: idempotence — re-promote + re-finalize add NOTHING ──────────

    @Test
    @Order(7)
    void rePromoteAndReFinalize_convergeNeverDuplicate() {
        int chunksBefore = count("SELECT count(*) FROM nexus.chunks");
        int manifestBefore = count("SELECT count(*) FROM nexus.catalog_document_chunks");
        int relevanceBefore = count("SELECT count(*) FROM nexus.relevance_log");

        Map<String, Object> again = ops.promoteCollection(T1, COLL_A, 768);
        assertThat(again.get("promoted")).as("re-promote inserts nothing").isEqualTo(0);
        ops.finalizeTenant(T1, false);

        assertThat(count("SELECT count(*) FROM nexus.chunks")).isEqualTo(chunksBefore);
        assertThat(count("SELECT count(*) FROM nexus.catalog_document_chunks")).isEqualTo(manifestBefore);
        assertThat(count("SELECT count(*) FROM nexus.relevance_log"))
            .as("the anti-join dedupe holds for the BIGSERIAL store").isEqualTo(relevanceBefore);
    }

    // ── Order 8: C2 — a LATE collection promotes + re-finalize covers it ─────

    @Test
    @Order(10)
    void unresolvableCanonicalManifestRow_staysStaged_neverDangles() {
        // Review P1 Critical scenario: a canonical-shaped staged pointer
        // whose content never landed (orphan-dropped upstream, or its
        // collection not yet promoted) must stay STAGED — the direct-decode
        // arm requires PROOF of content existence, so a dangling manifest
        // row cannot be created by finalize.
        String ghost = digestHex("content that never landed anywhere");
        scope.withTenant(T1, ctx -> {
            ctx.execute("INSERT INTO staging.document_chunks "
                + "(tenant_id, doc_id, position, chash) VALUES (?, '1.1.1', 7, ?) "
                + "ON CONFLICT DO NOTHING", T1, ghost);
            return null;
        });
        Map<String, Object> fin = ops.finalizeTenant(T1, false);
        assertThat(((Number) fin.get("manifest_unresolved")).intValue())
            .as("the ghost pointer is counted unresolved, not promoted")
            .isGreaterThanOrEqualTo(1);
        assertThat(fin.get("dangling_manifest")).isEqualTo(0);
        assertThat(count("SELECT count(*) FROM nexus.catalog_document_chunks "
            + "WHERE encode(chash,'hex') = '" + ghost + "'"))
            .as("no dangling manifest row was created").isEqualTo(0);
        scope.withTenant(T1, ctx -> {
            ctx.execute("DELETE FROM staging.document_chunks WHERE position = 7");
            return null;
        });
    }

    @Test
    @Order(11)
    void unresolvableKnowledgePointer_resyncsCountAndSurfacesTitle() {
        // nexus-b6enc F3: a store_put-origin doc (content_type='knowledge',
        // empty file_path) whose staged pointer cannot resolve has NO source
        // file to re-index from. The promote must (a) resync the doc's
        // verbatim-imported chunk_count down to the actually-promoted rows
        // and (b) surface the doc BY TITLE in the finalize envelope.
        String ghost = digestHex("knowledge note content that never landed");
        scope.withTenant(T1, ctx -> {
            ctx.execute("INSERT INTO nexus.catalog_documents "
                + "(tenant_id, tumbler, title, content_type, file_path, chunk_count) "
                + "VALUES (?, '5.5.5', 'orphaned-note-title', 'knowledge', '', 3) "
                + "ON CONFLICT DO NOTHING", T1);
            ctx.execute("INSERT INTO staging.document_chunks "
                + "(tenant_id, doc_id, position, chash) VALUES (?, '5.5.5', 0, ?) "
                + "ON CONFLICT DO NOTHING", T1, ghost);
            return null;
        });
        Map<String, Object> fin = ops.finalizeTenant(T1, false);
        assertThat(((Number) fin.get("chunk_count_resynced")).intValue())
            .as("the verbatim-imported count (3) must resync to the promoted rows (0)")
            .isGreaterThanOrEqualTo(1);
        assertThat(count("SELECT chunk_count FROM nexus.catalog_documents "
            + "WHERE tumbler = '5.5.5'"))
            .as("never trust the verbatim-imported count").isEqualTo(0);
        @SuppressWarnings("unchecked")
        List<String> titles = (List<String>) fin.get("unresolved_knowledge_titles");
        assertThat(titles)
            .as("the store_put-origin doc must be surfaced BY TITLE")
            .contains("orphaned-note-title");
        scope.withTenant(T1, ctx -> {
            ctx.execute("DELETE FROM staging.document_chunks WHERE doc_id = '5.5.5'");
            ctx.execute("DELETE FROM nexus.catalog_documents WHERE tumbler = '5.5.5'");
            return null;
        });
    }

    @Test
    @Order(11)
    void preExistingDanglingManifestRow_abortsFinalizeLoud() throws Exception {
        // The fatal gate's falsification (review P1 Critical: the count was
        // computed but never asserted — delete the throw and THIS fails).
        // nexus-7nrvr: catalog_document_chunks.collection is NOT NULL
        // (catalog-025-collection-not-null.xml). The dangling nature this row
        // exists to prove is about the CHASH (a ghost never landed anywhere,
        // "pre-existing corruption ghost" by construction) — orthogonal to
        // which collection value it carries. COLL_A is an arbitrary real,
        // already-registered collection in this fixture.
        String ghost = digestHex("pre-existing corruption ghost");
        // nexus-lgdel.l1: doc_id '1.1.1' was implicitly registered by the
        // now-deleted Order(6) test earlier in this ordered sequence; this
        // test's raw INSERT into catalog_document_chunks also carries an FK
        // to catalog_documents (fk_catalog_chunks_catalog_doc), so it must
        // register its own stub now.
        scope.withTenant(T1, ctx -> {
            ctx.execute("INSERT INTO nexus.catalog_documents (tenant_id, tumbler, title, physical_collection) "
                + "VALUES (?, '1.1.1', 'promote-doc', ?) ON CONFLICT DO NOTHING", T1, COLL_A);
            return null;
        });
        // RDR-191 Phase 5 (nexus-o8dil.29): fk_catalog_chunks_chunk now requires
        // a matching nexus.chunks row -- a genuinely-dangling row is exactly this
        // test's SUBJECT, so bypass the FK locally: drop the constraint, insert,
        // then re-add it NOT VALID (catalog-029-0's exact shape) so it is live
        // again (unvalidated) afterward.
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            su.createStatement().execute(
                "ALTER TABLE nexus.catalog_document_chunks DROP CONSTRAINT IF EXISTS fk_catalog_chunks_chunk");
            su.createStatement().execute(
                "INSERT INTO nexus.catalog_document_chunks (tenant_id, doc_id, position, chash, collection) "
                + "VALUES ('" + T1 + "', '1.1.1', 88, decode('" + ghost + "', 'hex'), '" + COLL_A + "')");
            su.createStatement().execute(
                "ALTER TABLE nexus.catalog_document_chunks "
                + "ADD CONSTRAINT fk_catalog_chunks_chunk "
                + "FOREIGN KEY (tenant_id, collection, chash) REFERENCES nexus.chunks (tenant_id, collection, chash) "
                + "ON UPDATE CASCADE DEFERRABLE INITIALLY IMMEDIATE NOT VALID");
        }
        try {
            org.assertj.core.api.Assertions.assertThatThrownBy(() -> ops.finalizeTenant(T1, false))
                .isInstanceOf(IllegalStateException.class)
                .hasMessageContaining("dangling manifest");
        } finally {
            try (Connection su = pg.createConnection("")) {
                su.setAutoCommit(true);
                su.createStatement().execute(
                    "DELETE FROM nexus.catalog_document_chunks WHERE position = 88");
            }
        }
        // And the census backstop sees the same class independently.
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            su.createStatement().execute(
                "ALTER TABLE nexus.catalog_document_chunks DROP CONSTRAINT IF EXISTS fk_catalog_chunks_chunk");
            su.createStatement().execute(
                "INSERT INTO nexus.catalog_document_chunks (tenant_id, doc_id, position, chash, collection) "
                + "VALUES ('" + T1 + "', '1.1.1', 89, decode('" + ghost + "', 'hex'), '" + COLL_A + "')");
            su.createStatement().execute(
                "ALTER TABLE nexus.catalog_document_chunks "
                + "ADD CONSTRAINT fk_catalog_chunks_chunk "
                + "FOREIGN KEY (tenant_id, collection, chash) REFERENCES nexus.chunks (tenant_id, collection, chash) "
                + "ON UPDATE CASCADE DEFERRABLE INITIALLY IMMEDIATE NOT VALID");
        }
        try {
            Map<String, Integer> residue = scope.withTenant(T1, ctx ->
                dev.nexus.service.db.ChashCensus.scan(ctx));
            assertThat(residue).containsKey("dangling.catalog_document_chunks");
        } finally {
            try (Connection su = pg.createConnection("")) {
                su.setAutoCommit(true);
                su.createStatement().execute(
                    "DELETE FROM nexus.catalog_document_chunks WHERE position = 89");
            }
        }
    }

    @Test
    @Order(9)
    void census_discoversKnownInventory_andFlagsANovelColumn() throws Exception {
        // Non-vacuity: the schema-derived enumeration rediscovers the known
        // chash-bearing inventory (a census that can't see its inventory is
        // broken) and every allowlist entry exists.
        scope.withTenant(T1, ctx -> {
            dev.nexus.service.db.ChashCensus.assertDiscoversKnownInventory(ctx);
            return null;
        });
        // THE missed-leg killer proof (Hal directive): seed legacy residue in
        // a NOVEL column no hand list has ever named — the census must find
        // it with zero code changes.
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            su.createStatement().execute(
                "CREATE TABLE nexus.census_canary (tenant_id TEXT NOT NULL DEFAULT '', "
                + "mystery_ref TEXT)");
            su.createStatement().execute(
                "GRANT SELECT ON nexus.census_canary TO " + SVC_ROLE);
            su.createStatement().execute(
                "INSERT INTO nexus.census_canary (tenant_id, mystery_ref) "
                + "VALUES ('" + T1 + "', '0123456789abcdef0123456789abcdef')");
        }
        try {
            Map<String, Integer> residue = scope.withTenant(T1, ctx ->
                dev.nexus.service.db.ChashCensus.scan(ctx));
            assertThat(residue)
                .as("a legacy-shaped value in a column NO hand list names must "
                    + "be discovered — the census is schema-derived or it is nothing")
                .containsEntry("census_canary.mystery_ref", 1);
        } finally {
            try (Connection su = pg.createConnection("")) {
                su.setAutoCommit(true);
                su.createStatement().execute("DROP TABLE nexus.census_canary");
            }
        }
        // Post-cleanup the migrated store scans clean.
        Map<String, Integer> clean = scope.withTenant(T1, ctx ->
            dev.nexus.service.db.ChashCensus.scan(ctx));
        assertThat(clean)
            .as("the promoted store must scan clean of legacy residue")
            .isEmpty();
    }

    @Test
    @Order(8)
    void lateCollection_afterFinalize_reFinalizePromotesItsPointers() {
        // nexus-lgdel.l1: the staged frecency pointer is now keyed by the
        // CANONICAL digest directly — legacy_ref-keyed pointer resolution
        // via chash_alias is retired (see Order 6's deletion note). This
        // still proves the surviving C2 capability: a late-landing
        // collection's pointer is unresolved (content not promoted yet) on
        // the first finalize and promotes cleanly on the RE-run once the
        // collection has promoted — "exactly once" is dead.
        String lateRef = "eeee4444eeee4444eeee4444eeee4444";  // M1 tiebreak input only
        String lateText = "late landed content";
        String lateCanon = digestHex(lateText);
        landChunk(COLL_LATE, 768, lateRef, lateText, vec(768));
        scope.withTenant(T1, ctx -> {
            ctx.execute("INSERT INTO staging.frecency (tenant_id, chunk_id, frecency_score) "
                + "VALUES (?, ?, 3.25) ON CONFLICT DO NOTHING", T1, lateCanon);
            return null;
        });

        ops.promoteCollection(T1, COLL_LATE, 768);
        Map<String, Object> fin = ops.finalizeTenant(T1, false);

        assertThat(count("SELECT count(*) FROM nexus.chunks "
            + "WHERE encode(chash,'hex') = '" + lateCanon + "'")).isEqualTo(1);
        assertThat(count("SELECT count(*) FROM nexus.frecency WHERE chunk_id = '" + lateCanon + "'"))
            .as("the late collection's pointer promoted on the RE-run — 'exactly once' is dead (C2)")
            .isEqualTo(1);
        assertThat(fin.get("residual_mismatched")).isEqualTo(0);
    }

    // nexus-lgdel.l1: Order 12 (finalizeWithAliasMapRemoved_
    // cannotResolveLegacyPointers_resumeConverges) DELETED — its subject was
    // a mutation-falsification proof that finalize depends on committed
    // chash_alias rows (DELETE FROM nexus.chash_alias to simulate the
    // alias-build never having run). The table itself no longer exists, so
    // this scenario cannot be constructed at all.

    // ── nexus-kmd5b: the dangling census must see LEGACY-WIDTH pointers ──────

    /**
     * The dangling-pointer legs gated on the CONFORMANT width, which excludes
     * exactly the population they exist to find: a pointer the cascade could
     * NOT repoint is, by definition, still at its legacy width. Production
     * 2026-07-20 measured the consequence — the chash_index leg reported
     * <strong>1</strong> against <strong>292,230</strong> actual orphans,
     * five orders of magnitude low, while the manifest leg (which carries no
     * width precondition) reported 426 against 426 actual.
     *
     * <p>Same structural shape as nexus-vounk: a check that cannot see the
     * thing it is checking for. Its "all clear" was not evidence of a clean
     * store, it was evidence of a blind query.
     *
     * <p>Seeds one dangling pointer per affected leg at LEGACY width — 16-byte
     * bytea for chash_index, 32-hex text for the three debt columns — with no
     * chash_alias entry, so none is resolvable by any route. Every leg must
     * report it. Pre-fix all four are silently invisible.
     */
    @Test
    @Order(13)
    void census_seesDanglingPointersAtLegacyWidth() throws Exception {
        // nexus-lgdel.l1: the relevance_log leg of this test is RETIRED —
        // nexus.relevance_log now carries a CHECK (chunk_id ~
        // '^[0-9a-f]{64}$'), so a legacy-width chunk_id can no longer be
        // seeded at all (the CHECK constraint itself is the fix for exactly
        // the class this leg used to prove needed a census). The
        // topic_assignments leg SURVIVES unchanged (no CHECK yet — its own
        // retirement belongs to nexus-tk070.p3d) and remains this test's
        // subject.
        final String legacyHex = "b".repeat(32);   // 16 bytes decoded
        long topicId;
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            su.createStatement().execute(
                "INSERT INTO nexus.catalog_collections (tenant_id, name) VALUES ('"
                + T1 + "', 'code__kmd5b') ON CONFLICT DO NOTHING");
            try (ResultSet rs = su.createStatement().executeQuery(
                    "INSERT INTO nexus.topics (tenant_id, label, collection, created_at) "
                    + "VALUES ('" + T1 + "', 'kmd5b', 'code__kmd5b', now()) RETURNING id")) {
                rs.next();
                topicId = rs.getLong(1);
            }
            su.createStatement().execute(
                "INSERT INTO nexus.topic_assignments "
                + "(tenant_id, doc_id, topic_id, assigned_by, source_collection) "
                + "VALUES ('" + T1 + "', '" + legacyHex + "', " + topicId + ", 'kmd5b', 'code__kmd5b')");
        }
        try {
            Map<String, Integer> residue = scope.withTenant(T1, ctx ->
                dev.nexus.service.db.ChashCensus.scan(ctx));
            assertThat(residue)
                .as("RDR-187 (nexus-piwya.5): the dangling.chash_index census leg is "
                    + "RETIRED ahead of the table DROP (nexus-piwya.9) — a leg reading "
                    + "nexus.chash_index would error on the missing relation once the "
                    + "router dies. The seeded orphan row is deliberately still here: "
                    + "the census must NOT report it (a resurrected leg fails this). "
                    + "The 292,230-orphan population this leg once counted dies at the "
                    + "DROP itself; the manifest + topic_assignments legs below remain "
                    + "the census's surface.")
                .doesNotContainKey("dangling.chash_index");
            assertThat(residue)
                .as("the 32-hex-shaped topic_assignments.doc_id is the same bug shape: "
                    + "a legacy-width pointer the cascade missed is excluded by a "
                    + "64-hex-only filter")
                .containsKeys("dangling.topic_assignments");
        } finally {
            try (Connection su = pg.createConnection("")) {
                su.setAutoCommit(true);
                su.createStatement().execute(
                    "DELETE FROM nexus.topic_assignments WHERE assigned_by = 'kmd5b'");
                su.createStatement().execute(
                    "DELETE FROM nexus.topics WHERE label = 'kmd5b'");
            }
        }
    }

    // nexus-lgdel.l1: census_doesNotFlagLegacyPointersTheAliasStillResolves
    // DELETED — its subject was a legacy-width pointer resolving through a
    // seeded nexus.chash_alias row (INSERT INTO nexus.chash_alias ...). The
    // table is dropped; this scenario cannot be constructed at all.

    // ── nexus-11gh6 post-review (T2 nexus/critique-11gh6-gate-impl-2026-08-08
    //    [21798] Critical finding): finalizeTenant's manifest INSERT is a
    //    catalog_document_chunks writer just like the 5 sites already gated
    //    in CatalogRepository — it must take acquireSweepGateShared for
    //    every DISTINCT target collection it resolves BEFORE the INSERT. ──

    private static final String COLL_GATE = "knowledge__kgate__bge-base-en-v15-768__v1";

    /** Raw connection to the service role's own pool (test-controlled transaction). */
    private Connection dsConnection() throws SQLException {
        return svcDs.getConnection();
    }

    /** Hand-drives {@code CatalogRepository.acquireSweepGateExclusive}'s exact
     *  SQL shape on a raw connection, for tests needing manual transaction control. */
    private static void acquireGateExclusive(Connection conn, String tenant, String collection, int lockTimeoutMs)
            throws SQLException {
        try (var ps = conn.prepareStatement("SELECT set_config('lock_timeout', ?, true)")) {
            ps.setString(1, String.valueOf(lockTimeoutMs));
            ps.execute();
        }
        try (var ps = conn.prepareStatement("SELECT pg_advisory_xact_lock(hashtext(?))")) {
            ps.setString(1, "sweepgate:" + tenant + "/" + collection);
            ps.execute();
        }
    }

    @Test
    @Order(20)
    void finalizeTenant_manifestInsert_blocksOnExternalExclusiveGate_thenProceeds() throws Exception {
        String canonicalText = "gate-test unique content " + System.nanoTime();
        String canonical = digestHex(canonicalText);
        landChunk(COLL_GATE, 768, canonical, canonicalText, vec(768));
        // Content lands live BEFORE finalize -- same sequencing every other
        // test in this file uses (promote, then finalize).
        Map<String, Object> promoted = ops.promoteCollection(T1, COLL_GATE, 768);
        assertThat(promoted.get("promoted")).isEqualTo(1);

        scope.withTenant(T1, ctx -> {
            ctx.execute("INSERT INTO nexus.catalog_documents "
                + "(tenant_id, tumbler, title, physical_collection) "
                + "VALUES (?, 'gate-doc-1', 'gate doc', ?) ON CONFLICT DO NOTHING",
                T1, COLL_GATE);
            ctx.execute("INSERT INTO staging.document_chunks "
                + "(tenant_id, doc_id, position, chash) VALUES (?, 'gate-doc-1', 0, ?) "
                + "ON CONFLICT DO NOTHING", T1, canonical);
            return null;
        });

        try (Connection external = dsConnection()) {
            external.setAutoCommit(false);
            try (var st = external.prepareStatement("SET nexus.tenant = '" + T1 + "'")) {
                st.execute();
            }
            // Generous lock_timeout on the EXTERNAL holder's own acquire --
            // it is uncontended, so this returns immediately; it never bounds
            // finalizeTenant's own wait (finalizeTenant takes the gate SHARED
            // with no timeout at all, by design -- see acquireSweepGateShared).
            acquireGateExclusive(external, T1, COLL_GATE, 60_000);

            ExecutorService executor = Executors.newSingleThreadExecutor();
            try {
                Future<Map<String, Object>> future = executor.submit(() -> ops.finalizeTenant(T1, false));
                assertThatThrownBy(() -> future.get(750, TimeUnit.MILLISECONDS))
                    .as("finalizeTenant's manifest INSERT must BLOCK while COLL_GATE's gate "
                        + "is held EXCLUSIVE externally -- a missing/broken gate call would let "
                        + "this complete immediately and this assertion would fail")
                    .isInstanceOf(TimeoutException.class);

                external.rollback();

                Map<String, Object> fin = future.get(15, TimeUnit.SECONDS);
                assertThat(fin.get("manifest_promoted"))
                    .as("gate released -- the manifest promote completes").isEqualTo(1);
            } finally {
                executor.shutdownNow();
            }
        }

        assertThat(count("SELECT count(*) FROM nexus.catalog_document_chunks "
            + "WHERE doc_id = 'gate-doc-1' AND encode(chash,'hex') = '" + canonical + "'"))
            .as("the manifest row landed once the gate was released").isEqualTo(1);
    }

    // ── nexus-11gh6 round 3 (T2 nexus/critique-11gh6-gate-impl-2026-08-08
    //    [21798] REWORK DELTA Critical finding): promoteCollection's OWN
    //    content-insert into chunks_<dim> was left ungated in round 2 on an
    //    incomplete exemption argument -- it must take the gate too. ──

    private static final String COLL_GATE2 = "knowledge__kgate2__bge-base-en-v15-768__v1";

    /** Hand-drives {@code CatalogRepository.acquireSweepGateShared}'s exact
     *  SQL shape on a raw connection, for tests needing manual transaction control. */
    private static void acquireGateShared(Connection conn, String tenant, String collection) throws SQLException {
        try (var ps = conn.prepareStatement("SELECT pg_advisory_xact_lock_shared(hashtext(?))")) {
            ps.setString(1, "sweepgate:" + tenant + "/" + collection);
            ps.execute();
        }
    }

    @Test
    @Order(21)
    void promoteCollection_contentInsert_blocksOnExternalExclusiveGate_thenProceeds() throws Exception {
        String text = "promote-gate-block content " + System.nanoTime();
        landChunk(COLL_GATE2, 768, digestHex(text), text, vec(768));

        try (Connection external = dsConnection()) {
            external.setAutoCommit(false);
            try (var st = external.prepareStatement("SET nexus.tenant = '" + T1 + "'")) {
                st.execute();
            }
            acquireGateExclusive(external, T1, COLL_GATE2, 60_000);

            ExecutorService executor = Executors.newSingleThreadExecutor();
            try {
                Future<Map<String, Object>> future =
                    executor.submit(() -> ops.promoteCollection(T1, COLL_GATE2, 768));
                assertThatThrownBy(() -> future.get(750, TimeUnit.MILLISECONDS))
                    .as("promoteCollection's content INSERT must BLOCK while COLL_GATE2's gate "
                        + "is held EXCLUSIVE externally -- a missing/broken gate call would let "
                        + "this complete immediately and this assertion would fail")
                    .isInstanceOf(TimeoutException.class);

                external.rollback();

                Map<String, Object> promoted = future.get(15, TimeUnit.SECONDS);
                assertThat(promoted.get("promoted")).as("gate released -- promote completes").isEqualTo(1);
            } finally {
                executor.shutdownNow();
            }
        }
    }

    @Test
    @Order(22)
    void promoteCollection_contentInsert_holdsGateAgainstConcurrentSweepGuard_rowSurvives() throws Exception {
        // Deterministic reproduction (mirrors CatalogManifestSweepRepositoryTest's
        // tripwire idiom): while promoteCollection's content-landing transaction
        // holds the gate SHARED for `collection`, an unrelated document's
        // concurrent sweep (which must take the SAME gate EXCLUSIVE before its
        // guarded DELETE can even run) cannot be granted -- so the freshly-landed
        // row SURVIVES the window, ready for a later finalizeTenant to manifest.
        // Hand-driven, not through ops.promoteCollection, so this test controls
        // the exact interleaving deterministically (no threads, no sleep/latch
        // luck) -- the entry-point gate call itself is proven separately by
        // Order(21) above.
        String col = "knowledge__kgate3__bge-base-en-v15-768__v1";
        String text = "round3 race content " + System.nanoTime();
        String chash = digestHex(text);

        try (Connection connA = dsConnection(); Connection connB = dsConnection()) {
            connA.setAutoCommit(false);
            connB.setAutoCommit(false);

            // Connection A: exactly what promoteCollection's fixed content-
            // insert step now does -- take the gate SHARED, then land the row
            // -- held UNCOMMITTED.
            try (var st = connA.prepareStatement("SET nexus.tenant = '" + T1 + "'")) {
                st.execute();
            }
            acquireGateShared(connA, T1, col);
            try (var st = connA.prepareStatement(
                    "INSERT INTO nexus.catalog_collections (tenant_id, name) VALUES (?, ?) "
                    + "ON CONFLICT DO NOTHING")) {
                st.setString(1, T1);
                st.setString(2, col);
                st.execute();
            }
            try (var ps = connA.prepareStatement(
                    "INSERT INTO nexus.chunks (tenant_id, collection, chash, chunk_text, embedding_768) "
                    + "VALUES (?, ?, decode(?, 'hex'), ?, ?::vector)")) {
                ps.setString(1, T1);
                ps.setString(2, col);
                ps.setString(3, chash);
                ps.setString(4, text);
                ps.setString(5, vec(768));
                ps.executeUpdate();
            }

            // Connection B: an unrelated document's ordinary sweep for the SAME
            // collection -- short lock_timeout, must be refused while A holds
            // SHARED, so its guarded DELETE never even runs.
            try (var st = connB.prepareStatement("SET nexus.tenant = '" + T1 + "'")) {
                st.execute();
            }
            assertThatThrownBy(() -> acquireGateExclusive(connB, T1, col, 1000))
                .as("a concurrent unrelated document's sweep must be refused the gate while "
                    + "promoteCollection's content-insert transaction holds it SHARED")
                .isInstanceOf(SQLException.class)
                .satisfies(e -> assertThat(((SQLException) e).getSQLState()).isEqualTo("55P03"));
            connB.rollback();

            connA.commit();
        }

        assertThat(count("SELECT count(*) FROM nexus.chunks WHERE collection = '" + col
            + "' AND encode(chash,'hex') = '" + chash + "'"))
            .as("the freshly-landed row survives the race and is ready for finalizeTenant "
                + "to manifest later").isEqualTo(1);
    }

    @Test
    @Order(23)
    void finalizeTenant_multiCollectionLoop_gatesEveryDistinctCollection() throws Exception {
        // nexus-11gh6 round 3: finalizeTenant resolves and gates potentially
        // MANY distinct collections in one call (one tenant-wide INSERT). A
        // regression that only gated the FIRST resolved collection (or a
        // hardcoded one) would let this call proceed even while a DIFFERENT
        // (second) collection's gate is held externally -- this test is
        // non-vacuous in exactly that direction.
        String colA = "knowledge__kmulti-a__bge-base-en-v15-768__v1";
        String colB = "knowledge__kmulti-b__bge-base-en-v15-768__v1";
        String textA = "multi-collection gate content A " + System.nanoTime();
        String textB = "multi-collection gate content B " + System.nanoTime();
        String chashA = digestHex(textA);
        String chashB = digestHex(textB);
        landChunk(colA, 768, chashA, textA, vec(768));
        landChunk(colB, 768, chashB, textB, vec(768));
        assertThat(ops.promoteCollection(T1, colA, 768).get("promoted")).isEqualTo(1);
        assertThat(ops.promoteCollection(T1, colB, 768).get("promoted")).isEqualTo(1);

        scope.withTenant(T1, ctx -> {
            ctx.execute("INSERT INTO nexus.catalog_documents "
                + "(tenant_id, tumbler, title, physical_collection) "
                + "VALUES (?, 'multi-doc-a', 'multi doc a', ?) ON CONFLICT DO NOTHING", T1, colA);
            ctx.execute("INSERT INTO nexus.catalog_documents "
                + "(tenant_id, tumbler, title, physical_collection) "
                + "VALUES (?, 'multi-doc-b', 'multi doc b', ?) ON CONFLICT DO NOTHING", T1, colB);
            ctx.execute("INSERT INTO staging.document_chunks "
                + "(tenant_id, doc_id, position, chash) VALUES (?, 'multi-doc-a', 0, ?) "
                + "ON CONFLICT DO NOTHING", T1, chashA);
            ctx.execute("INSERT INTO staging.document_chunks "
                + "(tenant_id, doc_id, position, chash) VALUES (?, 'multi-doc-b', 0, ?) "
                + "ON CONFLICT DO NOTHING", T1, chashB);
            return null;
        });

        // Hold colB's gate EXCLUSIVE externally. If the resolution loop only
        // gated ONE collection (e.g. the first resolved, or a hardcoded one),
        // this call would NOT block.
        try (Connection external = dsConnection()) {
            external.setAutoCommit(false);
            try (var st = external.prepareStatement("SET nexus.tenant = '" + T1 + "'")) {
                st.execute();
            }
            acquireGateExclusive(external, T1, colB, 60_000);

            ExecutorService executor = Executors.newSingleThreadExecutor();
            try {
                Future<Map<String, Object>> future = executor.submit(() -> ops.finalizeTenant(T1, false));
                assertThatThrownBy(() -> future.get(750, TimeUnit.MILLISECONDS))
                    .as("finalizeTenant must BLOCK on colB's gate too, not just colA's -- proving "
                        + "the multi-collection loop gates EVERY distinct collection it resolves, "
                        + "not just the first one")
                    .isInstanceOf(TimeoutException.class);

                external.rollback();

                Map<String, Object> fin = future.get(15, TimeUnit.SECONDS);
                assertThat(fin.get("manifest_promoted")).isEqualTo(2);
            } finally {
                executor.shutdownNow();
            }
        }

        assertThat(count("SELECT count(*) FROM nexus.catalog_document_chunks "
            + "WHERE doc_id = 'multi-doc-a' AND encode(chash,'hex') = '" + chashA + "'")).isEqualTo(1);
        assertThat(count("SELECT count(*) FROM nexus.catalog_document_chunks "
            + "WHERE doc_id = 'multi-doc-b' AND encode(chash,'hex') = '" + chashB + "'")).isEqualTo(1);
    }

    // nexus-lgdel.l1: Order 24
    // (finalizeTenant_aliasResolvedLegacyPointer_blocksOnExternalExclusiveGate_
    // thenProceeds) DELETED — its subject was proving the gate-resolution
    // query's ALIAS arm (COALESCE(a.new_chash, decode(s.chash,'hex')))
    // gates correctly for a legacy-shaped staged pointer. That arm is
    // REMOVED with nexus.chash_alias: the gate-resolution query's `cand`
    // derived table now has exactly one arm, the direct 64-hex decode,
    // already covered by every gate test through Order(23).

    // ── Order 25: F12b — finalize's manifest INSERT must stamp `collection` ──

    private static final String COLL_F12B = "knowledge__kf12b__bge-base-en-v15-768__v1";

    /**
     * nexus-o8dil.3 (RDR-191 F12b(ii)): {@code finalizeTenant}'s manifest
     * {@code INSERT...SELECT} never populated {@code
     * catalog_document_chunks.collection} — the 9-column list omitted it
     * entirely, unlike {@code promoteCollection}'s CONTENT insert, which DOES
     * stamp it (verified anchor sheet finding C). Every finalize-promoted
     * manifest row was a partial-NULL FK key: exactly the population GATE-2
     * (nexus-o8dil.7) requires to be zero before the FK can validate.
     *
     * <p>The fix stamps {@code collection} from {@code
     * catalog_documents.physical_collection} (NULL-if-empty) directly — the
     * one caller-known fact this bulk migration path has, mirroring the
     * explicit {@code collection} parameter every live writer
     * (CatalogRepository.writeManifestRows/appendManifestChunks/
     * importChunksBatch) now requires the caller to supply — not from
     * wherever the chash's content happens to physically live (which can
     * diverge under shared-chash reuse across collections — F10c/F8d
     * territory, not this bead's scope).
     */
    @Test
    @Order(25)
    void finalizeTenant_manifestInsert_stampsCollectionFromDocPhysicalCollection() {
        String text = "F12b manifest collection stamp content " + System.nanoTime();
        String canonical = digestHex(text);
        landChunk(COLL_F12B, 768, canonical, text, vec(768));
        Map<String, Object> promoted = ops.promoteCollection(T1, COLL_F12B, 768);
        assertThat(promoted.get("promoted")).isEqualTo(1);
        // The CONTENT insert's own stamp — this is the "same source the
        // content insert uses" the bead's acceptance criterion names; a
        // regression pin that promoteCollection's content leg still sets it.
        assertThat(count("SELECT count(*) FROM nexus.chunks "
            + "WHERE collection = '" + COLL_F12B + "' AND encode(chash,'hex') = '" + canonical + "'"))
            .as("regression pin: the CONTENT insert (StagingPromoteOps :410-435) "
                + "must be left untouched by this fix — it already stamps collection")
            .isEqualTo(1);

        scope.withTenant(T1, ctx -> {
            ctx.execute("INSERT INTO nexus.catalog_documents "
                + "(tenant_id, tumbler, title, physical_collection) "
                + "VALUES (?, 'f12b-doc', 'f12b doc', ?) ON CONFLICT DO NOTHING",
                T1, COLL_F12B);
            ctx.execute("INSERT INTO staging.document_chunks "
                + "(tenant_id, doc_id, position, chash) VALUES (?, 'f12b-doc', 0, ?) "
                + "ON CONFLICT DO NOTHING", T1, canonical);
            return null;
        });

        Map<String, Object> fin = ops.finalizeTenant(T1, false);
        assertThat(fin.get("manifest_promoted")).isEqualTo(1);

        assertThat(count("SELECT count(*) FROM nexus.catalog_document_chunks "
            + "WHERE doc_id = 'f12b-doc' AND encode(chash,'hex') = '" + canonical + "' "
            + "AND collection = '" + COLL_F12B + "'"))
            .as("F12b: the finalize manifest INSERT must stamp collection from the "
                + "owning document's physical_collection, the same caller-supplied-"
                + "collection contract every live writer now follows — a partial-"
                + "NULL FK key otherwise (RDR-191 GATE-2)")
            .isEqualTo(1);
    }

    // ── Order 26-28: nexus-o8dil.7 (RDR-191 GATE-2 review finding 3) —
    // StagingPromoteOps' case-1 and case-2 manifest-resolution logic had
    // ZERO test coverage before this. CatalogRepository's parallel fix got
    // five tests (ManifestCollectionStampTest); this class carries the
    // SAME two decisions in its own bulk INSERT...SELECT shape and must not
    // ship untested. ──

    private static final String COLL_G6 = "knowledge__kg6__bge-base-en-v15-768__v1";

    /**
     * Case 1 visibility (StagingPromoteOps :783-816 as of this fix): a
     * staged manifest pointer for a doc_id with NO catalog_documents row at
     * all is reported via the {@code manifest_doc_not_registered} counter
     * AND escalated to an explicit WARN log — neither half had any test
     * coverage before this (RDR-191 GATE-2 review finding 3).
     */
    @Test
    @Order(26)
    void finalizeTenant_manifestInsert_docNotRegistered_countsAndWarns() {
        String doc = "g6-not-registered";
        String chash = digestHex("g6 unregistered doc content " + System.nanoTime());
        scope.withTenant(T1, ctx -> {
            ctx.execute("INSERT INTO staging.document_chunks "
                + "(tenant_id, doc_id, position, chash) VALUES (?, ?, 0, ?) "
                + "ON CONFLICT DO NOTHING", T1, doc, chash);
            return null;
        });

        ch.qos.logback.classic.Logger root =
            (ch.qos.logback.classic.Logger) org.slf4j.LoggerFactory.getLogger(
                org.slf4j.Logger.ROOT_LOGGER_NAME);
        ch.qos.logback.core.read.ListAppender<ch.qos.logback.classic.spi.ILoggingEvent> logs =
            new ch.qos.logback.core.read.ListAppender<>();
        logs.start();
        root.addAppender(logs);
        Map<String, Object> fin;
        try {
            fin = ops.finalizeTenant(T1, false);
        } finally {
            root.detachAppender(logs);
            logs.stop();
        }

        assertThat(((Number) fin.get("manifest_doc_not_registered")).intValue())
            .as("the unregistered doc_id is counted")
            .isGreaterThanOrEqualTo(1);
        assertThat(count("SELECT count(*) FROM nexus.catalog_document_chunks WHERE doc_id = '" + doc + "'"))
            .as("no manifest row is fabricated for a doc that was never registered")
            .isEqualTo(0);

        var warnLines = logs.list.stream()
            .map(ch.qos.logback.classic.spi.ILoggingEvent::getFormattedMessage)
            .filter(m -> m.startsWith("event=staging_finalize_doc_not_registered"))
            .toList();
        assertThat(warnLines)
            .as("the counter is escalated to an explicit WARN, not left buried in "
                + "the per-tenant JSON envelope nobody is currently wired to read")
            .hasSize(1);
        assertThat(warnLines.getFirst())
            .contains("tenant=" + T1)
            .contains("count=" + fin.get("manifest_doc_not_registered"));

        scope.withTenant(T1, ctx -> {
            ctx.execute("DELETE FROM staging.document_chunks WHERE tenant_id = ? AND doc_id = ?", T1, doc);
            return null;
        });
    }

    /**
     * RDR-191 (Hal ruling 2026-08-12, nexus-j862l reconciliation): Order 27
     * originally asserted a ghost doc's staged pointer resolved its
     * collection from VERIFIED chash membership. Hal's final ruling
     * rejected that mechanism entirely — {@code StagingPromoteOps}
     * resolves a promoted row's {@code collection} SOLELY from the owning
     * document's own {@code physical_collection} (see
     * {@code docPhysicalCollection} in {@code finalizeTenant}), never from
     * where the chash's content happens to physically live. This test now
     * asserts THAT contract: a document with a REAL, non-empty {@code
     * physical_collection} gets its manifest row stamped with exactly that
     * value.
     */
    @Test
    @Order(27)
    void finalizeTenant_manifestInsert_docWithRealPhysicalCollection_stampsFromPhysicalCollection() {
        String doc = "g7.1";
        String newText = "g7 new content " + System.nanoTime();
        String newCanonical = digestHex(newText);

        scope.withTenant(T1, ctx -> {
            ctx.execute("INSERT INTO nexus.catalog_documents "
                + "(tenant_id, tumbler, title, physical_collection) "
                + "VALUES (?, ?, 'g7 doc', ?) ON CONFLICT DO NOTHING", T1, doc, COLL_G6);
            return null;
        });

        landChunk(COLL_G6, 768, newCanonical, newText, vec(768));
        assertThat(ops.promoteCollection(T1, COLL_G6, 768).get("promoted")).isEqualTo(1);

        scope.withTenant(T1, ctx -> {
            ctx.execute("INSERT INTO staging.document_chunks "
                + "(tenant_id, doc_id, position, chash) VALUES (?, ?, 0, ?) "
                + "ON CONFLICT DO NOTHING", T1, doc, newCanonical);
            return null;
        });

        ops.finalizeTenant(T1, false);
        assertThat(count("SELECT count(*) FROM nexus.catalog_document_chunks "
            + "WHERE doc_id = '" + doc + "' AND encode(chash,'hex') = '" + newCanonical + "' "
            + "AND collection = '" + COLL_G6 + "'"))
            .as("RDR-191: the manifest row is stamped from the document's own "
                + "physical_collection, unconditionally")
            .isEqualTo(1);
    }

    /**
     * RDR-191 (Hal ruling 2026-08-12, nexus-j862l reconciliation): Order 28
     * originally asserted a genuinely ambiguous chash (verified to live in
     * two different collections, with the doc's own sibling row naming
     * neither) stayed unresolved. That whole ambiguity concept no longer
     * exists — {@code finalizeTenant} never inspects where a chash's
     * content lives, so "ambiguous chash membership" cannot occur as a
     * distinguishable case any more. Re-based to assert the actual gate
     * that DOES leave a row unresolved under the shipped contract
     * (nexus-lyhac): a ghost document with an EMPTY {@code
     * physical_collection} produces NO manifest row, even when its staged
     * chash is genuinely resolvable (real chunk content exists and was
     * promoted) — {@code docPhysicalCollection.isNotNull()} is the gate,
     * not chash resolvability. The staged pointer must remain available
     * for a future finalize once the document is given a real collection.
     *
     * <p>nexus-0dkdx (substantive-critic round-2 finding, T2
     * nexus/critique-round2-nexus-j862l-test-reconciliation-2026-08-12
     * [22340]): this skip was silent — no counter, no WARN — unlike the
     * sibling {@code manifest_doc_not_registered} case (Order 26), which
     * got both in the same round. Extended (rather than split into a
     * sibling test) to assert the {@code manifest_doc_no_collection}
     * counter and its WARN log fire on exactly this already-exercised skip
     * path, mirroring Order 26's log-capture shape.
     */
    @Test
    @Order(28)
    void finalizeTenant_manifestInsert_ghostDoc_emptyPhysicalCollection_staysUnresolved() {
        String doc = "g8.1";
        String collZ = "knowledge__kg8z__bge-base-en-v15-768__v1";
        String text = "g8 content " + System.nanoTime();
        String canonical = digestHex(text);

        scope.withTenant(T1, ctx -> {
            ctx.execute("INSERT INTO nexus.catalog_documents "
                + "(tenant_id, tumbler, title, physical_collection) "
                + "VALUES (?, ?, 'g8 ghost doc', '') ON CONFLICT DO NOTHING", T1, doc);
            return null;
        });

        // The chash is genuinely resolvable -- real content, landed and
        // promoted -- so a resolvability check alone would not block it.
        landChunk(collZ, 768, canonical, text, vec(768));
        assertThat(ops.promoteCollection(T1, collZ, 768).get("promoted")).isEqualTo(1);

        scope.withTenant(T1, ctx -> {
            ctx.execute("INSERT INTO staging.document_chunks "
                + "(tenant_id, doc_id, position, chash) VALUES (?, ?, 1, ?) "
                + "ON CONFLICT DO NOTHING", T1, doc, canonical);
            return null;
        });

        ch.qos.logback.classic.Logger root =
            (ch.qos.logback.classic.Logger) org.slf4j.LoggerFactory.getLogger(
                org.slf4j.Logger.ROOT_LOGGER_NAME);
        ch.qos.logback.core.read.ListAppender<ch.qos.logback.classic.spi.ILoggingEvent> logs =
            new ch.qos.logback.core.read.ListAppender<>();
        logs.start();
        root.addAppender(logs);
        Map<String, Object> fin;
        try {
            fin = ops.finalizeTenant(T1, false);
        } finally {
            root.detachAppender(logs);
            logs.stop();
        }

        assertThat(count("SELECT count(*) FROM nexus.catalog_document_chunks "
            + "WHERE doc_id = '" + doc + "' AND encode(chash,'hex') = '" + canonical + "'"))
            .as("RDR-191: an empty physical_collection blocks the manifest "
                + "row even though the chash is genuinely resolvable -- "
                + "resolvability was never the gate")
            .isEqualTo(0);
        assertThat(count("SELECT count(*) FROM staging.document_chunks "
            + "WHERE tenant_id = '" + T1 + "' AND doc_id = '" + doc + "' AND position = 1"))
            .as("an unresolved row is never consumed from staging -- it stays "
                + "available for a future finalize")
            .isEqualTo(1);

        assertThat(((Number) fin.get("manifest_doc_no_collection")).intValue())
            .as("nexus-0dkdx: the registered-but-collection-less doc is counted")
            .isGreaterThanOrEqualTo(1);

        var warnLines = logs.list.stream()
            .map(ch.qos.logback.classic.spi.ILoggingEvent::getFormattedMessage)
            .filter(m -> m.startsWith("event=staging_finalize_doc_no_collection"))
            .toList();
        assertThat(warnLines)
            .as("nexus-0dkdx: the counter is escalated to an explicit WARN, "
                + "mirroring manifest_doc_not_registered's own escalation "
                + "(Order 26) rather than staying buried in the per-tenant "
                + "JSON envelope")
            .hasSize(1);
        assertThat(warnLines.getFirst())
            .contains("tenant=" + T1)
            .contains("count=" + fin.get("manifest_doc_no_collection"));

        scope.withTenant(T1, ctx -> {
            ctx.execute("DELETE FROM staging.document_chunks WHERE tenant_id = ? AND doc_id = ?", T1, doc);
            return null;
        });
    }

    // ── nexus-o8dil.50: orphan-synthesize dim coverage ───────────────────────
    //
    // Prior to this fix, finalizeTenant(tenant, true)'s orphan-synthesize
    // branch was hardcoded to chunks_768 only (inherited verbatim from the
    // deleted raw SQL). No test in this file ever called finalizeTenant
    // with synthesizeOrphans=true for ANY dim before nexus-o8dil.50 — the
    // three tests below are the first coverage of this branch at all, not
    // merely the first per-dim coverage. Each test proves: the orphan's
    // content row lands in the CORRECT dim column and no OTHER dim column.
    //
    // nexus-lgdel.l1: the SECOND half this comment used to describe — "a
    // manifest pointer that resolves through the orphan's synthetic alias
    // promotes cleanly" — is RETIRED along with nexus.chash_alias. The
    // synthetic chash is now computed directly (ChashSqlIdioms.digestField
    // over the same deterministic seed, recomputed per dim rather than
    // staged once and joined back), so there is no alias fact left for a
    // manifest pointer to resolve THROUGH; a staged manifest pointer whose
    // chash is the ORIGINAL legacy_ref text (not a 64-hex chash) never
    // resolves under the direct-hex-only manifestResolvable condition
    // either way, so it is no longer seeded here.
    //
    // RDR-191 repoint (nexus-o8dil.17): nexus.chunks_384/768/1024 collapsed
    // into ONE table, nexus.chunks, with a per-dim embedding_<dim> column.
    // The dim-coverage proof is phrased as "the CORRECT dim COLUMN and no
    // OTHER dim column" — see assertOrphanSynthesizesIntoDim's own comment
    // for why "table" became "column" without weakening what is proven.

    private void assertOrphanSynthesizesIntoDim(int dim, int order) {
        String coll = "knowledge__dimcheck" + dim + "__model-" + dim + "__v1";
        String doc = "9." + order + ".1";
        String legacyRef = "orphan-dim-" + dim + "-ref-" + order;

        scope.withTenant(T_DIM, ctx -> {
            ctx.execute("INSERT INTO nexus.catalog_documents "
                + "(tenant_id, tumbler, title, physical_collection) "
                + "VALUES (?, ?, 'dim-check doc', ?) ON CONFLICT DO NOTHING", T_DIM, doc, coll);
            // chunks_<dim> FK-references catalog_collections(tenant_id, name)
            // (fk-002-collection-registry.xml) — the orphan content INSERT
            // below needs the stub row promoteCollection's own step (4)
            // would normally create; this collection is never promoted
            // through that path so it must be stubbed directly here.
            ctx.execute("INSERT INTO nexus.catalog_collections (tenant_id, name, content_type) "
                + "VALUES (?, ?, 'knowledge') ON CONFLICT DO NOTHING", T_DIM, coll);
            return null;
        });
        // The orphan itself: an empty-text staged chunk row at this dim —
        // orphanCond requires no non-empty sibling row sharing the ref
        // (fresh ref here, so it holds).
        scope.withTenant(T_DIM, ctx -> {
            ctx.execute("INSERT INTO staging.chunks "
                + "(tenant_id, collection, dim, legacy_ref, chunk_text, embedding, model) "
                + "VALUES (?, ?, ?, ?, '', '" + vec(dim) + "'::vector, 'model-" + dim + "') "
                + "ON CONFLICT (tenant_id, collection, legacy_ref) DO NOTHING",
                T_DIM, coll, dim, legacyRef);
            return null;
        });

        Map<String, Object> fin = ops.finalizeTenant(T_DIM, true);

        assertThat(((Number) fin.get("orphans_synthesized")).intValue())
            .as("dim " + dim + ": exactly this one new orphan synthesizes")
            .isEqualTo(1);
        assertThat(fin.get("dangling_manifest")).isEqualTo(0);
        assertThat(fin.get("residual_mismatched")).isEqualTo(0);

        // nexus-lgdel.l1: the synthetic chash is now computed DIRECTLY from
        // the same deterministic seed StagingPromoteOps.finalizeTenant uses
        // (ChashSqlIdioms.digestField over "nexus:synthetic-chash:v1|"
        // + tenant + "|" + collection + "|" + legacyRef) — there is no
        // longer an alias row to JOIN through to locate the surrogate row.
        String synthChashHex = digestHex(
            "nexus:synthetic-chash:v1|" + T_DIM + "|" + coll + "|" + legacyRef);

        // RDR-191 repoint (nexus-o8dil.17): nexus.chunks_384/768/1024
        // collapsed into ONE table, nexus.chunks, with a per-dim
        // embedding_<dim> column (exactly one non-null, DB CHECK-enforced).
        // "landed in the dim-correct TABLE and nowhere else" is no longer
        // expressible -- there is only one table -- so the equivalent,
        // still-meaningful assertion is "landed in the dim-correct COLUMN":
        // embedding_<dim> IS NOT NULL on the surrogate row, and each OTHER
        // dim's embedding_<other> column carries NO row for this chash (the
        // direct analogue of "no cross-dim leakage into chunks_<other>" --
        // this is the actual regression a09e6b486 fixed: the orphan-
        // synthesize INSERT choosing the wrong embedding column, not the
        // wrong physical table).
        assertThat(countAs(T_DIM, "SELECT count(*) FROM nexus.chunks c "
            + "WHERE c.tenant_id = '" + T_DIM + "' AND encode(c.chash, 'hex') = '" + synthChashHex + "' "
            + "AND c.chunk_text = '' "
            + "AND c.metadata->>'chash_origin' = 'synthetic' "
            + "AND c.embedding_" + dim + " IS NOT NULL"))
            .as("dim " + dim + ": the surrogate content row landed with the "
                + "synthetic stamp AND its vector in the dim-correct column")
            .isEqualTo(1);
        for (int other : new int[] {384, 768, 1024}) {
            if (other == dim) continue;
            assertThat(countAs(T_DIM, "SELECT count(*) FROM nexus.chunks c "
                + "WHERE c.tenant_id = '" + T_DIM + "' AND encode(c.chash, 'hex') = '" + synthChashHex + "' "
                + "AND c.embedding_" + other + " IS NOT NULL"))
                .as("dim " + dim + ": no cross-dim leakage into embedding_" + other)
                .isEqualTo(0);
        }
    }

    @Test
    @Order(29)
    void orphanSynthesize_dim384_populatesChunks384AndResolvesManifest() {
        assertOrphanSynthesizesIntoDim(384, 29);
    }

    @Test
    @Order(30)
    void orphanSynthesize_dim768_populatesChunks768AndResolvesManifest() {
        assertOrphanSynthesizesIntoDim(768, 30);
    }

    @Test
    @Order(31)
    void orphanSynthesize_dim1024_populatesChunks1024AndResolvesManifest() {
        assertOrphanSynthesizesIntoDim(1024, 31);
    }

    // ── Order 32/33: nexus-cefa1.4 — document_aspects.extras promote cast ────
    //
    // finalizeTenant's document_aspects promote (Class-D, anti-join on
    // (collection, source_path)) selects the staged, still-TEXT extras column
    // into the now-jsonb DOCUMENT_ASPECTS.EXTRAS column via
    // StagingPromoteOps.parseStagedJson. No prior test in this file exercised
    // this leg at all (grepped: no staging.document_aspects INSERT anywhere
    // else in this file) — these two tests are the FIRST coverage of it.

    private static final String ASPECTS_PROMOTE_COLL = "aspects-promote-json-coll";

    @Test
    @Order(32)
    void finalizeTenant_documentAspectsPromote_castsStagedExtrasToJsonb() {
        // The staged column stays TEXT (staging is deliberately typeless) --
        // finalizeTenant's document_aspects promote must cast it explicitly
        // (StagingPromoteOps.parseStagedJson) or this INSERT ... SELECT fails
        // outright against the jsonb target column.
        //
        // doc_id must be a REGISTERED tumbler, not the staging column's own
        // NOT NULL DEFAULT '': nexus.document_aspects.doc_id carries a
        // (tenant_id, doc_id) FK to catalog_documents (fk-001), and unlike the
        // live write path (AspectRepository.nullIfBlank), this promote SELECT
        // passes the staged doc_id straight through with no blank-to-NULL
        // normalization -- an unregistered '' violates the FK. That gap is a
        // pre-existing Class-D promote behavior, out of scope for nexus-cefa1.4
        // (extras/salient_sentences only); route around it with a real tumbler.
        String doc = "aspects-promote-json-doc";
        scope.withTenant(T1, ctx -> {
            ctx.execute("INSERT INTO nexus.catalog_collections (tenant_id, name) VALUES (?, ?) "
                + "ON CONFLICT (tenant_id, name) DO NOTHING", T1, ASPECTS_PROMOTE_COLL);
            ctx.execute("INSERT INTO nexus.catalog_documents (tenant_id, tumbler, title) VALUES (?, ?, ?) "
                + "ON CONFLICT (tenant_id, tumbler) DO NOTHING", T1, doc, "aspects-promote-json fixture");
            ctx.execute("INSERT INTO staging.document_aspects "
                + "(tenant_id, doc_id, collection, source_path, extracted_at, model_version, "
                + "extractor_name, extras) VALUES (?, ?, ?, ?, '', 'v1', 'ex', ?)",
                T1, doc, ASPECTS_PROMOTE_COLL, "aspects-promote-json.pdf",
                "{\"venue\": \"VLDB\", \"year\": \"2023\"}");
            return null;
        });

        Map<String, Object> fin = ops.finalizeTenant(T1, false);
        assertThat(fin.get("document_aspects_promoted"))
            .as("the staged document_aspects row must promote (anti-join sees a new row)")
            .isEqualTo(1);

        String extrasText = scope.withTenant(T1, ctx -> ctx.fetchOne(
            "SELECT extras::text FROM nexus.document_aspects "
            + "WHERE tenant_id = ? AND collection = ? AND source_path = ?",
            T1, ASPECTS_PROMOTE_COLL, "aspects-promote-json.pdf").get(0, String.class));
        try {
            var mapper = new com.fasterxml.jackson.databind.ObjectMapper();
            @SuppressWarnings("unchecked")
            Map<String, Object> parsed = mapper.readValue(extrasText, Map.class);
            assertThat(parsed).containsEntry("venue", "VLDB").containsEntry("year", "2023");
        } catch (Exception e) {
            throw new AssertionError("promoted extras must remain parseable JSON: " + extrasText, e);
        }
    }

    @Test
    @Order(33)
    void finalizeTenant_documentAspectsPromote_malformedStagedExtras_failsLoud() {
        // The documented outcome (StagingPromoteOps.parseStagedJson's own javadoc,
        // and aspects-003-type-hygiene.xml's header): staging stays typeless by
        // design, so a malformed staged value fails LOUD at promote time rather
        // than landing silently -- the whole finalizeTenant transaction aborts,
        // and the malformed row never reaches nexus.document_aspects.
        String coll = ASPECTS_PROMOTE_COLL + "-malformed";
        // A REGISTERED doc_id, exactly like Order 32: this test must isolate the
        // JSON-cast failure from the blank-doc_id FK gap (nexus-5enca) -- with
        // doc_id='' both would raise and isInstanceOf(RuntimeException) could not
        // tell them apart (critique finding, cefa1.4).
        String doc = "aspects-promote-malformed-doc";
        scope.withTenant(T1, ctx -> {
            ctx.execute("INSERT INTO nexus.catalog_collections (tenant_id, name) VALUES (?, ?) "
                + "ON CONFLICT (tenant_id, name) DO NOTHING", T1, coll);
            ctx.execute("INSERT INTO nexus.catalog_documents (tenant_id, tumbler, title) VALUES (?, ?, ?) "
                + "ON CONFLICT (tenant_id, tumbler) DO NOTHING", T1, doc, "aspects-promote-malformed fixture");
            ctx.execute("INSERT INTO staging.document_aspects "
                + "(tenant_id, doc_id, collection, source_path, extracted_at, model_version, "
                + "extractor_name, extras) VALUES (?, ?, ?, ?, '', 'v1', 'ex', ?)",
                T1, doc, coll, "aspects-promote-malformed.pdf", "not-json-at-all");
            return null;
        });

        assertThatThrownBy(() -> ops.finalizeTenant(T1, false))
            .as("malformed staged extras must fail the promote loud, not land silently")
            .isInstanceOf(RuntimeException.class)
            .hasMessageContaining("json");

        int landed = count("SELECT count(*) FROM nexus.document_aspects "
            + "WHERE collection = '" + coll + "' AND source_path = 'aspects-promote-malformed.pdf'");
        assertThat(landed).as("the malformed row must NOT have landed in nexus.document_aspects")
            .isEqualTo(0);

        // Cleanup: T1's ordered sequence ends here, but stay consistent with
        // this file's own convention (Order 3/4) of cleaning up a fail-loud
        // scenario's staged row so no later finalize run would keep re-attempting
        // (and re-failing on) it.
        scope.withTenant(T1, ctx -> {
            ctx.execute("DELETE FROM staging.document_aspects WHERE collection = ?", coll);
            return null;
        });
    }

    // ── Order 34: RDR-194 D0.9 (nexus-tk070.p3a), non-conformant doc_id reject ──

    @Test
    @Order(34)
    void finalizeTenant_nonConformantTopicAssignmentDocId_rejectsByName() {
        // A staged doc_id that is not already a conformant 64-hex chash is
        // arbitrary text: a memory-note title, a historic tumbler, a
        // legacy 16/32-hex shape (nexus-lgdel.l1 tightened the reject to
        // cover this case too — chash_alias is retired, so a legacy shape
        // can never resolve and is non-conformant now, not merely staged-
        // pending), anything an ETL-era row could have carried. D0.9
        // requires finalize to fail loud, naming the offending value,
        // rather than pass it through to a column that can no longer hold
        // it once D1's bytea conversion lands.
        String badDocId = "some-memory-note-title-not-a-chash";
        scope.withTenant(T_REJECT, ctx -> {
            ctx.execute("INSERT INTO nexus.catalog_collections (tenant_id, name) "
                + "VALUES (?, ?) ON CONFLICT (tenant_id, name) DO NOTHING", T_REJECT, COLL_A);
            ctx.execute("INSERT INTO nexus.topics (tenant_id, label, collection, created_at) "
                + "VALUES (?, 'reject-topic', ?, now())", T_REJECT, COLL_A);
            ctx.execute("INSERT INTO staging.topic_assignments "
                + "(tenant_id, doc_id, topic_id, topic_label, topic_collection) "
                + "VALUES (?, ?, 999999, 'reject-topic', ?) ON CONFLICT DO NOTHING",
                T_REJECT, badDocId, COLL_A);
            return null;
        });

        assertThatThrownBy(() -> ops.finalizeTenant(T_REJECT, false))
            .as("a non-conformant staged doc_id must abort finalize loud, naming the value")
            .isInstanceOf(IllegalStateException.class)
            .hasMessageContaining(badDocId);

        assertThat(countAs(T_REJECT, "SELECT count(*) FROM nexus.topic_assignments "
            + "WHERE tenant_id = '" + T_REJECT + "'"))
            .as("the rejected row must never reach nexus.topic_assignments")
            .isEqualTo(0);
    }
}
