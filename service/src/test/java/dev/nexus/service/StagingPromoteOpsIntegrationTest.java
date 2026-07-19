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
            // FK parent for the assignment, then the assignment (chash-keyed).
            ctx.execute("INSERT INTO nexus.topics "
                + "(tenant_id, label, collection, created_at) VALUES (?, 'topic-x', ?, now())",
                T1, COLL_A);
            Long topicId = ctx.fetchOne(
                "SELECT id FROM nexus.topics WHERE label = 'topic-x'").get(0, Long.class);
            ctx.execute("INSERT INTO staging.topic_assignments (tenant_id, doc_id, topic_id) "
                + "VALUES (?, ?, ?) ON CONFLICT DO NOTHING", T1, legacy32, topicId);
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
        assertThat(count("SELECT count(*) FROM nexus.topic_assignments WHERE doc_id = '" + canon1 + "'"))
            .as("assignment repointed to the canonical hex").isEqualTo(1);
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
}
