package dev.nexus.service;

import dev.nexus.service.db.StagingPromoteOps;
import dev.nexus.service.db.StagingPromoteOps.PromoteConflictException;
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
 * C1 (cross-collection alias contradiction fails loud against COMMITTED
 * state), C2 (finalize is idempotent + re-runnable; a LATE collection's
 * pointers promote on the next finalize), C4 (a reference-only row whose
 * content sibling lives in a DIFFERENT collection resolves, never drops),
 * M1 (collapse pair promotes deterministically to ONE row, both refs
 * aliased), H1 (staged dim disagreeing with the name-implied dim refuses),
 * R5 (promote into a populated target converges; re-promote adds nothing).
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
    void promote_allWidths_landAtDigests_aliasesBuilt() {
        String legacy16 = "b46c7915c303245f";                       // pre-RDR-108
        String legacy32 = legacy32(TEXT_1);                          // RDR-108 era
        String canonical = digestHex(TEXT_2);                        // already canonical
        landChunk(COLL_A, 768, legacy16, "sixteen char content", vec(768));
        landChunk(COLL_A, 768, legacy32, TEXT_1, vec(768));
        landChunk(COLL_A, 768, canonical, TEXT_2, vec(768));

        Map<String, Object> counts = ops.promoteCollection(T1, COLL_A, 768);
        assertThat(counts.get("promoted")).isEqualTo(3);
        assertThat(counts.get("alias_rows"))
            .as("only the two GENUINELY legacy refs alias; the canonical ref maps to itself")
            .isEqualTo(2);

        assertThat(count("SELECT count(*) FROM nexus.chunks_768 "
            + "WHERE collection = '" + COLL_A + "' AND octet_length(chash) = 32"))
            .isEqualTo(3);
        // Both legacy refs resolve through the alias to their digests.
        assertThat(count("SELECT count(*) FROM nexus.chash_alias "
            + "WHERE old_ref = '" + legacy16 + "' AND encode(new_chash,'hex') = '"
            + digestHex("sixteen char content") + "'")).isEqualTo(1);
        assertThat(count("SELECT count(*) FROM nexus.chash_alias "
            + "WHERE old_ref = '" + legacy32 + "' AND encode(new_chash,'hex') = '"
            + digestHex(TEXT_1) + "'")).isEqualTo(1);
        // RDR-086 metadata parity (--guided gate run 3 catch, nexus-
        // jxizy.10.10): serving-path writes stamp chunk_text_hash into
        // metadata client-side; the citation resolver's final hop
        // (/v1/vectors/get where={"chunk_text_hash": ...}) filters on it.
        // Promoted rows must be indistinguishable from serving-path writes,
        // so promote stamps the digest hex at INSERT — a verbatim
        // chunk_meta copy leaves every migrated chunk invisible to
        // citations.
        assertThat(count("SELECT count(*) FROM nexus.chunks_768 "
            + "WHERE collection = '" + COLL_A + "' "
            + "AND metadata->>'chunk_text_hash' IS DISTINCT FROM encode(chash,'hex')"))
            .as("every promoted row's metadata chunk_text_hash mirrors its chash")
            .isEqualTo(0);
    }

    // ── Order 2: collapse pair (M1) — one row, both refs aliased ─────────────

    @Test
    @Order(2)
    void promote_collapsePair_oneRowBothAliased_deterministicKeeper() {
        String refX = "aaaa0000aaaa0000aaaa0000aaaa0000";
        String refY = "bbbb1111bbbb1111bbbb1111bbbb1111";
        landChunk(COLL_A, 768, refX, TEXT_DUP, vec(768));
        landChunk(COLL_A, 768, refY, TEXT_DUP, vec(768));

        Map<String, Object> counts = ops.promoteCollection(T1, COLL_A, 768);
        assertThat(count("SELECT count(*) FROM nexus.chunks_768 "
            + "WHERE encode(chash,'hex') = '" + digestHex(TEXT_DUP) + "'"))
            .as("identical text collapses to ONE content row").isEqualTo(1);
        for (String r : new String[] {refX, refY}) {
            assertThat(count("SELECT count(*) FROM nexus.chash_alias WHERE old_ref = '" + r + "'"))
                .as("both collapse-pair refs alias to the shared digest").isEqualTo(1);
        }
    }

    // ── Order 3: C1 — committed-alias contradiction fails loud ───────────────

    @Test
    @Order(3)
    void promote_sameRefDifferentContentAcrossCollections_failsLoud() {
        String sharedRef = legacy32(TEXT_1);   // already aliased to TEXT_1's digest (order 1)
        landChunk(COLL_B, 768, sharedRef, "entirely different content", vec(768));

        assertThatThrownBy(() -> ops.promoteCollection(T1, COLL_B, 768))
            .isInstanceOf(PromoteConflictException.class)
            .hasMessageContaining(sharedRef)
            .hasMessageContaining("refusing to pick silently");
        // Cleanup so later finalize runs see a consistent staging set.
        scope.withTenant(T1, ctx -> {
            ctx.execute("DELETE FROM staging.chunks WHERE collection = ?", COLL_B);
            return null;
        });
    }

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

    // ── Order 6: finalize — manifest + pointers + Item8 cross-collection ─────

    @Test
    @Order(6)
    void finalize_promotesPointers_resolvesCrossCollectionReference() {
        String legacy32 = legacy32(TEXT_1);
        // A reference-only row in COLL_B whose content sibling landed in
        // COLL_A (order 1) — the C4 scenario: must RESOLVE, never drop.
        landChunk(COLL_B, 768, legacy32, "", vec(768));
        scope.withTenant(T1, ctx -> {
            // The manifest FKs to catalog_documents (fk_catalog_chunks_
            // catalog_doc — RDR-156 schema-enforced integrity): docs are
            // tumbler-keyed, non-chash, and migrate via the EXISTING catalog
            // ETL BEFORE finalize (a sequencer ordering fact for P2.2). The
            // IT stands in for that leg here.
            // nexus-7nrvr: real collection — ghost-ness was incidental
            // (cross-collection reference resolution is the point). COLL_A
            // is this doc's real home: that is where promoteCollection(T1,
            // COLL_A, 768) below lands its promoted rows and where the
            // topic-x assignment resolves.
            ctx.execute("INSERT INTO nexus.catalog_documents (tenant_id, tumbler, title, physical_collection) "
                + "VALUES (?, '1.1.1', 'promote-doc', ?) ON CONFLICT DO NOTHING", T1, COLL_A);
            // Manifest rows: one legacy-ref pointer, one already-canonical.
            ctx.execute("INSERT INTO staging.document_chunks "
                + "(tenant_id, doc_id, position, chash) VALUES (?, '1.1.1', 0, ?) "
                + "ON CONFLICT DO NOTHING", T1, legacy32);
            ctx.execute("INSERT INTO staging.document_chunks "
                + "(tenant_id, doc_id, position, chash) VALUES (?, '1.1.1', 1, ?) "
                + "ON CONFLICT DO NOTHING", T1, digestHex(TEXT_2));
            // (staging.chash_index seed removed — RDR-187/nexus-piwya.11:
            // the dead-sink landing twin is dropped by rdr187-002; old
            // clients' landing attempts now 400 at /v1/staging/load.)
            // THE CROSS-ID-SPACE SCENARIO (critic-p1 Critical): the staged
            // assignment carries a LEGACY integer id (424242 — some SQLite
            // BIGSERIAL value that can never exist in nexus.topics) plus the
            // (label, collection) identity; the target topic has its OWN
            // serial id. Finalize must resolve by identity, never by the
            // legacy integer.
            ctx.execute("INSERT INTO nexus.topics "
                + "(tenant_id, label, collection, created_at) VALUES (?, 'topic-x', ?, now())",
                T1, COLL_A);
            ctx.execute("INSERT INTO staging.topic_assignments "
                + "(tenant_id, doc_id, topic_id, topic_label, topic_collection) "
                + "VALUES (?, ?, 424242, 'topic-x', ?) ON CONFLICT DO NOTHING",
                T1, legacy32, COLL_A);
            // And one whose topic has NOT landed: stays staged, counted.
            ctx.execute("INSERT INTO staging.topic_assignments "
                + "(tenant_id, doc_id, topic_id, topic_label, topic_collection) "
                + "VALUES (?, ?, 424243, 'topic-never-landed', ?) ON CONFLICT DO NOTHING",
                T1, legacy32, COLL_A);
            // Frecency + relevance keyed by the legacy ref.
            ctx.execute("INSERT INTO staging.frecency (tenant_id, chunk_id, frecency_score) "
                + "VALUES (?, ?, 7.5) ON CONFLICT DO NOTHING", T1, legacy32);
            ctx.execute("INSERT INTO staging.relevance_log "
                + "(tenant_id, id, query, chunk_id, action, ts) "
                + "VALUES (?, 1, 'q1', ?, 'hit', '2026-07-01T00:00:00Z') ON CONFLICT DO NOTHING",
                T1, legacy32);
            return null;
        });

        // RDR-187 (nexus-piwya.7): the chash_index promote leg is retired —
        // the per-collection promote must leave the landed rows unpromoted
        // (asserted below).
        ops.promoteCollection(T1, COLL_A, 768);
        Map<String, Object> fin = ops.finalizeTenant(T1, false);

        assertThat(fin.get("reference_only_resolved"))
            .as("the COLL_B empty-text row's ref resolves through COLL_A's alias (C4)")
            .isEqualTo(1);
        assertThat(fin.get("orphans_dropped")).isEqualTo(0);
        assertThat(fin.get("manifest_promoted")).isEqualTo(2);
        assertThat(fin.get("residual_mismatched")).isEqualTo(0);
        assertThat(fin.get("dangling_manifest")).isEqualTo(0);

        String canon1 = digestHex(TEXT_1);
        assertThat(count("SELECT count(*) FROM nexus.catalog_document_chunks "
            + "WHERE doc_id = '1.1.1' AND encode(chash,'hex') = '" + canon1 + "'"))
            .as("the legacy manifest pointer promoted CANONICAL").isEqualTo(1);
        assertThat(count("SELECT count(*) FROM information_schema.tables "
            + "WHERE table_schema = 'nexus' AND table_name = 'chash_index'"))
            .as("RDR-187 (nexus-piwya.7/.9): the staging chash promote leg is RETIRED "
                + "and the router TABLE is dropped. The chunks promote (4) is the "
                + "registration.")
            .isZero();
        assertThat(count("SELECT count(*) FROM information_schema.tables "
            + "WHERE table_schema = 'staging' AND table_name = 'chash_index'"))
            .as("RDR-187 (nexus-piwya.11): the staging landing twin is dropped "
                + "by rdr187-002 — nothing can land, nothing can promote.")
            .isZero();
        assertThat(count("SELECT count(*) FROM nexus.topic_assignments ta "
            + "JOIN nexus.topics t ON t.id = ta.topic_id "
            + "WHERE ta.doc_id = '" + canon1 + "' AND t.label = 'topic-x'"))
            .as("assignment repointed to the canonical hex AND resolved to the "
                + "TARGET topic's own serial id via (label, collection) — never "
                + "the legacy integer").isEqualTo(1);
        assertThat(count("SELECT count(*) FROM nexus.topic_assignments WHERE topic_id = 424242"))
            .as("the legacy integer id never enters nexus").isEqualTo(0);
        assertThat(((Number) fin.get("topic_assignments_unresolved")).intValue())
            .as("the not-yet-landed topic's assignment stays staged, counted")
            .isEqualTo(1);
        assertThat(count("SELECT count(*) FROM nexus.frecency WHERE chunk_id = '" + canon1 + "'"))
            .isEqualTo(1);
        assertThat(count("SELECT count(*) FROM nexus.relevance_log WHERE chunk_id = '" + canon1 + "'"))
            .isEqualTo(1);
    }

    // ── Order 7: idempotence — re-promote + re-finalize add NOTHING ──────────

    @Test
    @Order(7)
    void rePromoteAndReFinalize_convergeNeverDuplicate() {
        int chunksBefore = count("SELECT count(*) FROM nexus.chunks_768");
        int aliasBefore = count("SELECT count(*) FROM nexus.chash_alias");
        int manifestBefore = count("SELECT count(*) FROM nexus.catalog_document_chunks");
        int relevanceBefore = count("SELECT count(*) FROM nexus.relevance_log");

        Map<String, Object> again = ops.promoteCollection(T1, COLL_A, 768);
        assertThat(again.get("promoted")).as("re-promote inserts nothing").isEqualTo(0);
        ops.finalizeTenant(T1, false);

        assertThat(count("SELECT count(*) FROM nexus.chunks_768")).isEqualTo(chunksBefore);
        assertThat(count("SELECT count(*) FROM nexus.chash_alias")).isEqualTo(aliasBefore);
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
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            su.createStatement().execute(
                "INSERT INTO nexus.catalog_document_chunks (tenant_id, doc_id, position, chash, collection) "
                + "VALUES ('" + T1 + "', '1.1.1', 88, decode('" + ghost + "', 'hex'), '" + COLL_A + "')");
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
                "INSERT INTO nexus.catalog_document_chunks (tenant_id, doc_id, position, chash, collection) "
                + "VALUES ('" + T1 + "', '1.1.1', 89, decode('" + ghost + "', 'hex'), '" + COLL_A + "')");
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
        String lateRef = "eeee4444eeee4444eeee4444eeee4444";
        String lateText = "late landed content";
        landChunk(COLL_LATE, 768, lateRef, lateText, vec(768));
        scope.withTenant(T1, ctx -> {
            ctx.execute("INSERT INTO staging.frecency (tenant_id, chunk_id, frecency_score) "
                + "VALUES (?, ?, 3.25) ON CONFLICT DO NOTHING", T1, lateRef);
            return null;
        });

        ops.promoteCollection(T1, COLL_LATE, 768);
        Map<String, Object> fin = ops.finalizeTenant(T1, false);

        String lateCanon = digestHex(lateText);
        assertThat(count("SELECT count(*) FROM nexus.chunks_768 "
            + "WHERE encode(chash,'hex') = '" + lateCanon + "'")).isEqualTo(1);
        assertThat(count("SELECT count(*) FROM nexus.frecency WHERE chunk_id = '" + lateCanon + "'"))
            .as("the late collection's pointer promoted on the RE-run — 'exactly once' is dead (C2)")
            .isEqualTo(1);
        assertThat(fin.get("residual_mismatched")).isEqualTo(0);
    }

    // ── Order 12: MUTATION FALSIFICATION — alias-build is load-bearing at the
    // WRITE path (critic-1010, nexus-jxizy.10.10 item 5). The rehearsal's
    // Phase-5 falsification proves the READ path (citation resolution)
    // depends on the alias map persisting; this proves the FINALIZE path
    // depends on it at execution time: in the world where the alias-build
    // statement never ran (its entire effect — the alias rows — removed),
    // the resolvable-only manifest promote MUST leave the legacy pointer
    // staged. Then the idempotent resume (re-promote rebuilds the facts,
    // re-finalize converges) proves recovery.

    @Test
    @Order(12)
    void finalizeWithAliasMapRemoved_cannotResolveLegacyPointers_resumeConverges() {
        String collM = "knowledge__mutation__bge-base-en-v15-768__v1";
        String text = "mutation falsification content";
        String ref = legacy32(text);
        landChunk(collM, 768, ref, text, vec(768));
        ops.promoteCollection(T1, collM, 768);
        scope.withTenant(T1, ctx -> {
            // nexus-7nrvr: real collection — ghost-ness was incidental
            // (alias-map mutation-falsification behaviour is the point).
            // collM is this doc's real home: that is where the content it
            // references was landed and promoted.
            ctx.execute("INSERT INTO nexus.catalog_documents (tenant_id, tumbler, title, physical_collection) "
                + "VALUES (?, '1.1.9', 'mutation-doc', ?) ON CONFLICT DO NOTHING", T1, collM);
            ctx.execute("INSERT INTO staging.document_chunks "
                + "(tenant_id, doc_id, position, chash) VALUES (?, '1.1.9', 0, ?) "
                + "ON CONFLICT DO NOTHING", T1, ref);
            // THE MUTATION: remove the alias-build's entire effect (RLS scopes
            // this to the test tenant).
            ctx.execute("DELETE FROM nexus.chash_alias");
            return null;
        });

        Map<String, Object> fin = ops.finalizeTenant(T1, false);
        assertThat(count("SELECT count(*) FROM nexus.catalog_document_chunks "
            + "WHERE doc_id = '1.1.9'"))
            .as("with the alias map gone the legacy manifest pointer CANNOT promote "
                + "(resolvable-only) — finalize is load-bearing on alias-build")
            .isZero();
        assertThat((int) fin.get("manifest_unresolved")).isGreaterThanOrEqualTo(1);

        // Resume: re-promote rebuilds the alias facts from the retained
        // staging rows; re-finalize converges the pointer.
        ops.promoteCollection(T1, collM, 768);
        ops.finalizeTenant(T1, false);
        assertThat(count("SELECT count(*) FROM nexus.catalog_document_chunks "
            + "WHERE doc_id = '1.1.9' AND encode(chash,'hex') = '" + digestHex(text) + "'"))
            .as("idempotent resume converges the pointer once the alias facts return")
            .isEqualTo(1);
    }

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
        final String legacyHex = "b".repeat(32);   // 16 bytes decoded
        long topicId;
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            su.createStatement().execute(
                "INSERT INTO nexus.catalog_collections (tenant_id, name) VALUES ('"
                + T1 + "', 'code__kmd5b') ON CONFLICT DO NOTHING");
            // (nexus.chash_index seed removed — RDR-187/nexus-piwya.9: the
            // router table is dropped; the doesNotContainKey pin below now
            // guards against a resurrected LEG against a resurrected TABLE
            // both, and the debt-column legs remain the live subjects.)
            try (ResultSet rs = su.createStatement().executeQuery(
                    "INSERT INTO nexus.topics (tenant_id, label, collection, created_at) "
                    + "VALUES ('" + T1 + "', 'kmd5b', 'code__kmd5b', now()) RETURNING id")) {
                rs.next();
                topicId = rs.getLong(1);
            }
            su.createStatement().execute(
                "INSERT INTO nexus.topic_assignments (tenant_id, doc_id, topic_id, assigned_by) "
                + "VALUES ('" + T1 + "', '" + legacyHex + "', " + topicId + ", 'kmd5b')");
            su.createStatement().execute(
                "INSERT INTO nexus.relevance_log (tenant_id, query, chunk_id, action, timestamp) "
                + "VALUES ('" + T1 + "', 'kmd5b', '" + legacyHex + "', 'view', now())");
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
                    + "DROP itself; the manifest + debt-column legs below remain the "
                    + "census's surface.")
                .doesNotContainKey("dangling.chash_index");
            assertThat(residue)
                .as("the 32-hex debt columns are the same bug shape: a legacy-width "
                    + "pointer the cascade missed is excluded by a 64-hex-only filter")
                .containsKeys("dangling.topic_assignments", "dangling.relevance_log");
        } finally {
            try (Connection su = pg.createConnection("")) {
                su.setAutoCommit(true);
                su.createStatement().execute(
                    "DELETE FROM nexus.topic_assignments WHERE assigned_by = 'kmd5b'");
                su.createStatement().execute(
                    "DELETE FROM nexus.topics WHERE label = 'kmd5b'");
                su.createStatement().execute(
                    "DELETE FROM nexus.relevance_log WHERE query = 'kmd5b'");
            }
        }
    }

    /**
     * The other half of the kmd5b contract: widening the legs must not turn
     * every LEGACY-BUT-RESOLVABLE pointer into a false orphan. A legacy-width
     * pointer WITH a chash_alias entry pointing at a live chunk resolves fine
     * — that is exactly what the permanent alias map is for (RDR-180: legacy
     * references stay resolvable forever) — so it must NOT be reported.
     * Without this, the fix would trade a blind check for a screaming one and
     * the census would flag the entire pre-rekey era.
     */
    @Test
    @Order(14)
    void census_doesNotFlagLegacyPointersTheAliasStillResolves() throws Exception {
        final String legacyHex = "c".repeat(32);
        final String text = "kmd5b alias-resolvable chunk";
        final String liveHex = digestHex(text);
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            su.createStatement().execute(
                "INSERT INTO nexus.catalog_collections (tenant_id, name) VALUES ('"
                + T1 + "', 'code__kmd5b2') ON CONFLICT DO NOTHING");
            su.createStatement().execute(
                "INSERT INTO nexus.chunks_768 (tenant_id, collection, chash, chunk_text, embedding) "
                + "VALUES ('" + T1 + "', 'code__kmd5b2', decode('" + liveHex + "', 'hex'), '"
                + text + "', '" + vec(768) + "'::vector)");
            su.createStatement().execute(
                "INSERT INTO nexus.chash_alias (tenant_id, old_ref, old_bytes, new_chash, source) "
                + "VALUES ('" + T1 + "', '" + legacyHex + "', decode('" + legacyHex + "', 'hex'), "
                + "decode('" + liveHex + "', 'hex'), 'kmd5b2')");
            // (nexus.chash_index seed removed — RDR-187/nexus-piwya.9: the
            // router is dropped; relevance_log carries the alias-resolvable
            // not-flagged proof.)
            su.createStatement().execute(
                "INSERT INTO nexus.relevance_log (tenant_id, query, chunk_id, action, timestamp) "
                + "VALUES ('" + T1 + "', 'kmd5b2', '" + legacyHex + "', 'view', now())");
        }
        try {
            Map<String, Integer> residue = scope.withTenant(T1, ctx ->
                dev.nexus.service.db.ChashCensus.scan(ctx));
            assertThat(residue)
                .as("a legacy pointer the alias map RESOLVES to a live chunk is not "
                    + "dangling — widening the leg must not flag the whole legacy era")
                .doesNotContainKeys("dangling.relevance_log");
        } finally {
            try (Connection su = pg.createConnection("")) {
                su.setAutoCommit(true);
                su.createStatement().execute("DELETE FROM nexus.relevance_log WHERE query = 'kmd5b2'");
                su.createStatement().execute("DELETE FROM nexus.chash_alias WHERE source = 'kmd5b2'");
                su.createStatement().execute(
                    "DELETE FROM nexus.chunks_768 WHERE collection = 'code__kmd5b2'");
            }
        }
    }

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
                    "INSERT INTO nexus.chunks_768 (tenant_id, collection, chash, chunk_text, embedding) "
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

        assertThat(count("SELECT count(*) FROM nexus.chunks_768 WHERE collection = '" + col
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

    // ── nexus-4okz4 increment 1, critic ROUND 3 pin-sensitivity finding
    //    (T2 critique-t76bp-rekey-gate-2026-08-08 [21807], "Site 3 ALIAS
    //    branch is unpinned"): finalizeTenant's gate-target resolution
    //    query (StagingPromoteOps.java site 3) has TWO candidate arms --
    //    `COALESCE(a.new_chash, decode(s.chash, 'hex'))` -- the alias arm
    //    (a.new_chash, taken when a LEGACY staged chash resolves through
    //    chash_alias) and the direct-hex decode arm (taken when the staged
    //    chash already IS the 64-hex canonical digest). Every gate-blocking
    //    fixture through Order(23) stages a CANONICAL 64-hex chash
    //    (`digestHex(text)`), so every one of them flows through the
    //    decode arm only -- a rendering bug that silently emptied the
    //    ALIAS arm (e.g. a wrong CHASH_ALIAS.OLD_REF join field, or an
    //    inverted `.isNotNull()`) would under-gate a LEGACY-pointer
    //    finalize and still pass all of Order(20)-(23). THIS test stages a
    //    LEGACY (32-hex, non-canonical-width) ref instead, so
    //    `s.chash ~ '^[0-9a-f]{64}$'` is false and the ONLY way the query
    //    can resolve a target collection is through `a.new_chash`. ──

    private static final String COLL_GATE4 = "knowledge__kgate4__bge-base-en-v15-768__v1";

    @Test
    @Order(24)
    void finalizeTenant_aliasResolvedLegacyPointer_blocksOnExternalExclusiveGate_thenProceeds()
            throws Exception {
        String text = "alias-branch gate content " + System.nanoTime();
        String legacyRef = legacy32(text);
        String canonical = digestHex(text);
        landChunk(COLL_GATE4, 768, legacyRef, text, vec(768));
        // Promote builds the chash_alias fact (old_ref=legacyRef ->
        // new_chash=canonical) AND lands the physical content row --
        // finalize's manifest promote below relies on BOTH.
        Map<String, Object> promoted = ops.promoteCollection(T1, COLL_GATE4, 768);
        assertThat(promoted.get("promoted")).isEqualTo(1);
        assertThat(promoted.get("alias_rows"))
            .as("legacyRef is genuinely non-canonical -- it must alias").isEqualTo(1);

        scope.withTenant(T1, ctx -> {
            ctx.execute("INSERT INTO nexus.catalog_documents "
                + "(tenant_id, tumbler, title, physical_collection) "
                + "VALUES (?, 'gate-doc-4', 'gate doc 4', ?) ON CONFLICT DO NOTHING",
                T1, COLL_GATE4);
            // The staged manifest pointer carries the LEGACY ref VERBATIM,
            // not the canonical digest -- this is what forces the query
            // through the alias arm (`a.new_chash IS NOT NULL`) rather than
            // the direct-hex decode arm every earlier gate test exercises.
            ctx.execute("INSERT INTO staging.document_chunks "
                + "(tenant_id, doc_id, position, chash) VALUES (?, 'gate-doc-4', 0, ?) "
                + "ON CONFLICT DO NOTHING", T1, legacyRef);
            return null;
        });

        try (Connection external = dsConnection()) {
            external.setAutoCommit(false);
            try (var st = external.prepareStatement("SET nexus.tenant = '" + T1 + "'")) {
                st.execute();
            }
            acquireGateExclusive(external, T1, COLL_GATE4, 60_000);

            ExecutorService executor = Executors.newSingleThreadExecutor();
            try {
                Future<Map<String, Object>> future = executor.submit(() -> ops.finalizeTenant(T1, false));
                assertThatThrownBy(() -> future.get(750, TimeUnit.MILLISECONDS))
                    .as("finalizeTenant must BLOCK on COLL_GATE4's gate for an ALIAS-resolved "
                        + "legacy pointer too, not just a direct-hex-canonical one -- a rendering "
                        + "bug that emptied the alias arm would let this complete immediately "
                        + "and this assertion would fail")
                    .isInstanceOf(TimeoutException.class);

                external.rollback();

                Map<String, Object> fin = future.get(15, TimeUnit.SECONDS);
                assertThat(fin.get("manifest_promoted"))
                    .as("gate released -- the alias-resolved manifest promote completes")
                    .isEqualTo(1);
            } finally {
                executor.shutdownNow();
            }
        }

        assertThat(count("SELECT count(*) FROM nexus.catalog_document_chunks "
            + "WHERE doc_id = 'gate-doc-4' AND encode(chash,'hex') = '" + canonical + "'"))
            .as("the manifest row landed at the ALIAS-RESOLVED canonical digest, proving the "
                + "alias arm actually ran (not merely the decode arm)")
            .isEqualTo(1);
    }

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
        assertThat(count("SELECT count(*) FROM nexus.chunks_768 "
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
    // merely the first per-dim coverage. Each test proves two things
    // together: (a) the orphan's content row lands in the CORRECT dim
    // table and nowhere else, and (b) a manifest pointer that resolves
    // through the orphan's synthetic alias promotes cleanly instead of
    // tripping the dangling_manifest fatal check — the actual production
    // hazard the pre-fix hardcoding created for 384/1024 (a committed
    // alias with no backing content anywhere, aborting the WHOLE
    // finalizeTenant transaction the moment anything referenced it).

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
        // orphanCond requires no pre-existing alias and no non-empty
        // sibling row sharing the ref (neither holds here, fresh ref).
        scope.withTenant(T_DIM, ctx -> {
            ctx.execute("INSERT INTO staging.chunks "
                + "(tenant_id, collection, dim, legacy_ref, chunk_text, embedding, model) "
                + "VALUES (?, ?, ?, ?, '', '" + vec(dim) + "'::vector, 'model-" + dim + "') "
                + "ON CONFLICT (tenant_id, collection, legacy_ref) DO NOTHING",
                T_DIM, coll, dim, legacyRef);
            return null;
        });
        // A manifest pointer referencing the SAME legacy ref — the Item8
        // scenario: this can only resolve through the synthetic alias
        // finalize is about to build.
        scope.withTenant(T_DIM, ctx -> {
            ctx.execute("INSERT INTO staging.document_chunks "
                + "(tenant_id, doc_id, position, chash) VALUES (?, ?, 0, ?) "
                + "ON CONFLICT DO NOTHING", T_DIM, doc, legacyRef);
            return null;
        });

        Map<String, Object> fin = ops.finalizeTenant(T_DIM, true);

        assertThat(((Number) fin.get("orphans_synthesized")).intValue())
            .as("dim " + dim + ": exactly this one new orphan synthesizes")
            .isEqualTo(1);
        assertThat(fin.get("dangling_manifest"))
            .as("dim " + dim + ": the manifest row that resolves through the "
                + "synthetic alias has real backing content -- the fatal "
                + "dangling-manifest abort this bug used to trip for "
                + "384/1024 must not fire")
            .isEqualTo(0);
        assertThat(fin.get("residual_mismatched")).isEqualTo(0);

        String targetTable = "nexus.chunks_" + dim;
        assertThat(countAs(T_DIM, "SELECT count(*) FROM " + targetTable + " c "
            + "JOIN nexus.chash_alias a ON a.new_chash = c.chash "
            + "WHERE c.tenant_id = '" + T_DIM + "' AND a.old_ref = '" + legacyRef + "' "
            + "AND a.source = 'staging:synthetic' AND c.chunk_text = '' "
            + "AND c.metadata->>'chash_origin' = 'synthetic'"))
            .as("dim " + dim + ": the surrogate content row landed in the "
                + "dim-correct table with the synthetic stamp")
            .isEqualTo(1);
        for (int other : new int[] {384, 768, 1024}) {
            if (other == dim) continue;
            assertThat(countAs(T_DIM, "SELECT count(*) FROM nexus.chunks_" + other + " c "
                + "JOIN nexus.chash_alias a ON a.new_chash = c.chash "
                + "WHERE a.old_ref = '" + legacyRef + "'"))
                .as("dim " + dim + ": no cross-dim leakage into chunks_" + other)
                .isEqualTo(0);
        }
        assertThat(countAs(T_DIM, "SELECT count(*) FROM nexus.catalog_document_chunks "
            + "WHERE tenant_id = '" + T_DIM + "' AND doc_id = '" + doc + "'"))
            .as("dim " + dim + ": the referencing manifest pointer promoted "
                + "instead of aborting or staying stuck")
            .isEqualTo(1);
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
}
