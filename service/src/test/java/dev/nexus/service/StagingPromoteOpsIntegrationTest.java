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
import java.util.HexFormat;
import java.util.Map;

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
            ctx.execute("INSERT INTO nexus.catalog_documents (tenant_id, tumbler, title) "
                + "VALUES (?, '1.1.1', 'promote-doc') ON CONFLICT DO NOTHING", T1);
            // Manifest rows: one legacy-ref pointer, one already-canonical.
            ctx.execute("INSERT INTO staging.document_chunks "
                + "(tenant_id, doc_id, position, chash) VALUES (?, '1.1.1', 0, ?) "
                + "ON CONFLICT DO NOTHING", T1, legacy32);
            ctx.execute("INSERT INTO staging.document_chunks "
                + "(tenant_id, doc_id, position, chash) VALUES (?, '1.1.1', 1, ?) "
                + "ON CONFLICT DO NOTHING", T1, digestHex(TEXT_2));
            // chash_index rows for COLL_A (promoted per-collection).
            ctx.execute("INSERT INTO staging.chash_index "
                + "(tenant_id, chash, physical_collection, created_at) "
                + "VALUES (?, ?, ?, '2026-07-01T00:00:00Z') ON CONFLICT DO NOTHING",
                T1, legacy32, COLL_A);
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

        // The chash_index leg rides the per-collection promote.
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
        assertThat(count("SELECT count(*) FROM nexus.chash_index "
            + "WHERE encode(chash,'hex') = '" + canon1 + "' AND physical_collection = '" + COLL_A + "'"))
            .isEqualTo(1);
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
    void preExistingDanglingManifestRow_abortsFinalizeLoud() throws Exception {
        // The fatal gate's falsification (review P1 Critical: the count was
        // computed but never asserted — delete the throw and THIS fails).
        String ghost = digestHex("pre-existing corruption ghost");
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            su.createStatement().execute(
                "INSERT INTO nexus.catalog_document_chunks (tenant_id, doc_id, position, chash) "
                + "VALUES ('" + T1 + "', '1.1.1', 88, decode('" + ghost + "', 'hex'))");
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
                "INSERT INTO nexus.catalog_document_chunks (tenant_id, doc_id, position, chash) "
                + "VALUES ('" + T1 + "', '1.1.1', 89, decode('" + ghost + "', 'hex'))");
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
            ctx.execute("INSERT INTO nexus.catalog_documents (tenant_id, tumbler, title) "
                + "VALUES (?, '1.1.9', 'mutation-doc') ON CONFLICT DO NOTHING", T1);
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
            // Model production exactly: the row PREDATES the octet CHECK, which
            // is NOT VALID and therefore gates only new writes. Drop, seed, restore.
            su.createStatement().execute(
                "ALTER TABLE nexus.chash_index DROP CONSTRAINT chash_index_chash_octet_check");
            su.createStatement().execute(
                "INSERT INTO nexus.chash_index (tenant_id, chash, physical_collection, created_at) "
                + "VALUES ('" + T1 + "', decode('" + legacyHex + "', 'hex'), 'code__kmd5b', now())");
            su.createStatement().execute(
                "ALTER TABLE nexus.chash_index ADD CONSTRAINT chash_index_chash_octet_check "
                + "CHECK (octet_length(chash) = 32) NOT VALID");
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
                .as("a 16-byte chash_index pointer resolving to NO chunk and carrying NO "
                    + "alias entry is dangling — the width precondition made this leg "
                    + "blind to its own target (reported 1 vs 292,230 in production)")
                .containsKey("dangling.chash_index");
            assertThat(residue)
                .as("the 32-hex debt columns are the same bug shape: a legacy-width "
                    + "pointer the cascade missed is excluded by a 64-hex-only filter")
                .containsKeys("dangling.topic_assignments", "dangling.relevance_log");
        } finally {
            try (Connection su = pg.createConnection("")) {
                su.setAutoCommit(true);
                su.createStatement().execute(
                    "DELETE FROM nexus.chash_index WHERE physical_collection = 'code__kmd5b'");
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
            su.createStatement().execute(
                "ALTER TABLE nexus.chash_index DROP CONSTRAINT chash_index_chash_octet_check");
            su.createStatement().execute(
                "INSERT INTO nexus.chash_index (tenant_id, chash, physical_collection, created_at) "
                + "VALUES ('" + T1 + "', decode('" + legacyHex + "', 'hex'), 'code__kmd5b2', now())");
            su.createStatement().execute(
                "ALTER TABLE nexus.chash_index ADD CONSTRAINT chash_index_chash_octet_check "
                + "CHECK (octet_length(chash) = 32) NOT VALID");
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
                .doesNotContainKeys("dangling.chash_index", "dangling.relevance_log");
        } finally {
            try (Connection su = pg.createConnection("")) {
                su.setAutoCommit(true);
                su.createStatement().execute(
                    "DELETE FROM nexus.chash_index WHERE physical_collection = 'code__kmd5b2'");
                su.createStatement().execute("DELETE FROM nexus.relevance_log WHERE query = 'kmd5b2'");
                su.createStatement().execute("DELETE FROM nexus.chash_alias WHERE source = 'kmd5b2'");
                su.createStatement().execute(
                    "DELETE FROM nexus.chunks_768 WHERE collection = 'code__kmd5b2'");
            }
        }
    }
}
