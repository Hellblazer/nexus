package dev.nexus.service;

import dev.nexus.service.db.CatalogRepository;
import dev.nexus.service.db.ChashSqlIdioms;
import dev.nexus.service.db.StagingPromoteOps;
import dev.nexus.service.db.StagingPromoteOps.PromotePreconditionException;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.jooq.binding.Vector;
import dev.nexus.service.jooq.binding.VectorBinding;
import dev.nexus.service.vectors.DimTables;
import org.jooq.DSLContext;
import org.jooq.Field;
import org.jooq.JSONB;
import org.jooq.SQLDialect;
import org.jooq.Table;
import org.jooq.exception.DataAccessException;
import org.jooq.impl.DSL;
import org.jooq.impl.SQLDataType;
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
import java.sql.SQLException;
import java.time.OffsetDateTime;
import java.util.HexFormat;
import java.util.List;
import java.util.Map;
import java.util.function.Function;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.TimeoutException;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.DOCUMENT_ASPECTS;
import static dev.nexus.service.jooq.nexus.Tables.FRECENCY;
import static dev.nexus.service.jooq.nexus.Tables.RELEVANCE_LOG;
import static dev.nexus.service.jooq.nexus.Tables.TOPICS;
import static dev.nexus.service.jooq.nexus.Tables.TOPIC_ASSIGNMENTS;
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

    /** {@link #vec}'s all-zero pgvector value as a typed {@link Vector}, for jOOQ
     *  inserts against the generated {@code CHUNKS.EMBEDDING_&lt;dim&gt;} columns
     *  (nexus-cbo4a batch 4) -- a {@code float[]} defaults to all-zero components. */
    private static Vector zeroVector(int dim) {
        return Vector.of(new float[dim]);
    }

    // ── staging.* fixed-shape typed handles (nexus-cbo4a batch 11) ───────────
    // staging.* carries no generated jOOQ Table -- jOOQ codegen's <schemata>
    // only covers nexus/t1 (staging is a typeless landing area, never a
    // serving-path table) -- so every column below is a plain
    // DSL.field(DSL.name(colName), ...) handle, the same house pattern
    // StagingHandler/StagingPromoteOps/CatalogRepository already use for
    // staging.* access (nexus-t76bp).

    private static final Table<?> STAGING_CHUNKS = DSL.table(DSL.name("staging", "chunks"));
    private static final Field<String> SC_TENANT_ID = DSL.field(DSL.name("tenant_id"), String.class);
    private static final Field<String> SC_COLLECTION = DSL.field(DSL.name("collection"), String.class);
    private static final Field<Integer> SC_DIM = DSL.field(DSL.name("dim"), Integer.class);
    private static final Field<String> SC_LEGACY_REF = DSL.field(DSL.name("legacy_ref"), String.class);
    private static final Field<String> SC_CHUNK_TEXT = DSL.field(DSL.name("chunk_text"), String.class);
    private static final Field<Vector> SC_EMBEDDING = DSL.field(DSL.name("embedding"),
        SQLDataType.OTHER.asConvertedDataType(new VectorBinding()));
    private static final Field<String> SC_MODEL = DSL.field(DSL.name("model"), String.class);

    private static final Table<?> STAGING_DOCUMENT_CHUNKS = DSL.table(DSL.name("staging", "document_chunks"));
    private static final Field<String> SDC_TENANT_ID = DSL.field(DSL.name("tenant_id"), String.class);
    private static final Field<String> SDC_DOC_ID = DSL.field(DSL.name("doc_id"), String.class);
    private static final Field<Integer> SDC_POSITION = DSL.field(DSL.name("position"), Integer.class);
    private static final Field<String> SDC_CHASH = DSL.field(DSL.name("chash"), String.class);

    private static final Table<?> STAGING_FRECENCY = DSL.table(DSL.name("staging", "frecency"));
    private static final Field<String> SF_TENANT_ID = DSL.field(DSL.name("tenant_id"), String.class);
    private static final Field<String> SF_CHUNK_ID = DSL.field(DSL.name("chunk_id"), String.class);
    private static final Field<Double> SF_FRECENCY_SCORE = DSL.field(DSL.name("frecency_score"), Double.class);

    private static final Table<?> STAGING_DOCUMENT_ASPECTS = DSL.table(DSL.name("staging", "document_aspects"));
    private static final Field<String> SDA_TENANT_ID = DSL.field(DSL.name("tenant_id"), String.class);
    private static final Field<String> SDA_DOC_ID = DSL.field(DSL.name("doc_id"), String.class);
    private static final Field<String> SDA_COLLECTION = DSL.field(DSL.name("collection"), String.class);
    private static final Field<String> SDA_SOURCE_PATH = DSL.field(DSL.name("source_path"), String.class);
    private static final Field<String> SDA_EXTRACTED_AT = DSL.field(DSL.name("extracted_at"), String.class);
    private static final Field<String> SDA_MODEL_VERSION = DSL.field(DSL.name("model_version"), String.class);
    private static final Field<String> SDA_EXTRACTOR_NAME = DSL.field(DSL.name("extractor_name"), String.class);
    private static final Field<String> SDA_SOURCE_URI = DSL.field(DSL.name("source_uri"), String.class);
    private static final Field<String> SDA_EXTRAS = DSL.field(DSL.name("extras"), String.class);

    private static final Table<?> STAGING_TOPIC_ASSIGNMENTS = DSL.table(DSL.name("staging", "topic_assignments"));
    private static final Field<String> STA_TENANT_ID = DSL.field(DSL.name("tenant_id"), String.class);
    private static final Field<String> STA_DOC_ID = DSL.field(DSL.name("doc_id"), String.class);
    private static final Field<Long> STA_TOPIC_ID = DSL.field(DSL.name("topic_id"), Long.class);
    private static final Field<String> STA_TOPIC_LABEL = DSL.field(DSL.name("topic_label"), String.class);
    private static final Field<String> STA_TOPIC_COLLECTION = DSL.field(DSL.name("topic_collection"), String.class);

    /** {@code CHUNKS.embedding_<dim>} resolved by name (nexus-cbo4a batch 11) -- the
     *  same {@code DimTables.embeddingColumn}-driven lookup {@code StagingPromoteOps}
     *  itself uses to pick the dim-correct generated column. */
    @SuppressWarnings("unchecked")
    private static Field<Vector> embeddingColumn(int dim) {
        return (Field<Vector>) CHUNKS.field(DimTables.embeddingColumn(dim));
    }

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.bootstrapServiceRole(su, SVC_ROLE, SVC_PASS);
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

        // RDR-204 Phase 1 (bead nexus-ft04v.7): chunks_collection_fk is a REAL,
        // always-enforced FK to catalog_collections — StagingPromoteOps.
        // promoteCollection's stub-insert is retired, so a real row must exist
        // before the content INSERT into chunks_<dim>, or the FK itself rejects it.
        // This suite's promoteCollection tests predate that requirement and use a
        // small, distinct, per-test collection name purely for row isolation, not to
        // exercise registration behavior itself (CollectionRegistryTest owns the
        // fail-loud contract). Registered here, once, under BOTH tenants this file
        // promotes against (RLS isolation is a DATA guarantee, never a registration
        // one). Tests that ALREADY register their own collection explicitly via a
        // real catalog_collections INSERT (finalizeTenant paths not reached through
        // promoteCollection) are unaffected either way.
        for (String col : List.of(COLL_A, COLL_B, COLL_LATE, COLL_GATE, COLL_GATE2,
                COLL_F12B, COLL_G6,
                "knowledge__kmulti-a__bge-base-en-v15-768__v1",
                "knowledge__kmulti-b__bge-base-en-v15-768__v1",
                "knowledge__kg8z__bge-base-en-v15-768__v1")) {
            for (String tenant : List.of(T1, T_REJECT)) {
                scope.withTenant(tenant, ctx -> {
                    PgContainerHelper.insertCollection(ctx, tenant, col);
                    return null;
                });
            }
        }
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    private void landChunk(String coll, int dim, String ref, String text, String vecLit) {
        Vector v = vecLit == null ? null : Vector.parse(vecLit);
        scope.withTenant(T1, ctx -> {
            ctx.insertInto(STAGING_CHUNKS, SC_TENANT_ID, SC_COLLECTION, SC_DIM, SC_LEGACY_REF, SC_CHUNK_TEXT,
                           SC_EMBEDDING, SC_MODEL)
               .values(T1, coll, dim, ref, text, v, "bge-768")
               .onConflict(SC_TENANT_ID, SC_COLLECTION, SC_LEGACY_REF)
               .doUpdate()
               .set(SC_CHUNK_TEXT, DSL.excluded(SC_CHUNK_TEXT))
               .execute();
            return null;
        });
    }

    /**
     * Runs {@code query} against a T1-scoped {@link DSLContext} and returns its
     * {@code int} result (nexus-cbo4a batch 11 -- retires the {@code count(String
     * sql)} raw-SQL wrapper the same way {@code CatalogRenameCollectionTest}'s
     * {@code rows(Connection, String)} was retired in batch 10: every call site now
     * builds its own typed jOOQ query rather than a SQL-string literal).
     */
    private int count(Function<DSLContext, ? extends Number> query) {
        return scope.withTenant(T1, ctx -> query.apply(ctx).intValue());
    }

    // nexus-o8dil.50: count() above is hardcoded to T1 -- every nexus.* table
    // is FORCE ROW LEVEL SECURITY on current_setting('nexus.tenant'), so a
    // query run under T1's session context returns ZERO rows for T_DIM data
    // regardless of an explicit `tenant_id = ...` predicate in the SQL text
    // (RLS filters before the WHERE clause is evaluated against visible
    // rows). The dim-coverage tests below run under T_DIM and need this
    // tenant-parameterized twin.
    private int countAs(String tenant, Function<DSLContext, ? extends Number> query) {
        return scope.withTenant(tenant, ctx -> query.apply(ctx).intValue());
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

        assertThat(count(ctx -> ctx.selectCount().from(CHUNKS)
            .where(CHUNKS.COLLECTION.eq(COLL_A))
            // Field#octetLength() is a deprecated STRING-oriented convenience
            // (casts to varchar first, per AbstractField#varchar()/#octetLength()) --
            // wrong for a byte[]/bytea column. octet_length(bytea) needs the
            // built-in Postgres function invoked directly against the raw field.
            .and(DSL.function("octet_length", SQLDataType.INTEGER, CHUNKS.CHASH).eq(32))
            .fetchOne(0, Integer.class)))
            .isEqualTo(3);
        assertThat(count(ctx -> ctx.selectCount().from(CHUNKS)
            .where(CHUNKS.COLLECTION.eq(COLL_A))
            .and(CHUNKS.CHASH.eq(HexFormat.of().parseHex(digestHex("sixteen char content"))))
            .fetchOne(0, Integer.class))).isEqualTo(1);
        assertThat(count(ctx -> ctx.selectCount().from(CHUNKS)
            .where(CHUNKS.COLLECTION.eq(COLL_A))
            .and(CHUNKS.CHASH.eq(HexFormat.of().parseHex(digestHex(TEXT_1))))
            .fetchOne(0, Integer.class))).isEqualTo(1);
        // RDR-086 metadata parity (--guided gate run 3 catch, nexus-
        // jxizy.10.10): serving-path writes stamp chunk_text_hash into
        // metadata client-side; the citation resolver's final hop
        // (/v1/vectors/get where={"chunk_text_hash": ...}) filters on it.
        // Promoted rows must be indistinguishable from serving-path writes,
        // so promote stamps the digest hex at INSERT — a verbatim
        // chunk_meta copy leaves every migrated chunk invisible to
        // citations.
        assertThat(count(ctx -> ctx.selectCount().from(CHUNKS)
            .where(CHUNKS.COLLECTION.eq(COLL_A))
            .and(DSL.jsonbGetAttributeAsText(CHUNKS.METADATA, "chunk_text_hash")
                .isDistinctFrom(DSL.function("encode", String.class, CHUNKS.CHASH, DSL.val("hex"))))
            .fetchOne(0, Integer.class)))
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
        assertThat(count(ctx -> ctx.selectCount().from(CHUNKS)
            .where(CHUNKS.CHASH.eq(HexFormat.of().parseHex(digestHex(TEXT_DUP))))
            .fetchOne(0, Integer.class)))
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
            ctx.deleteFrom(STAGING_CHUNKS).where(SC_COLLECTION.eq(COLL_B)).execute();
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
            ctx.deleteFrom(STAGING_CHUNKS).where(SC_COLLECTION.eq(COLL_B)).execute();
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
        int chunksBefore = count(ctx -> ctx.selectCount().from(CHUNKS).fetchOne(0, Integer.class));
        int manifestBefore = count(ctx -> ctx.selectCount().from(CATALOG_DOCUMENT_CHUNKS).fetchOne(0, Integer.class));
        int relevanceBefore = count(ctx -> ctx.selectCount().from(RELEVANCE_LOG).fetchOne(0, Integer.class));

        Map<String, Object> again = ops.promoteCollection(T1, COLL_A, 768);
        assertThat(again.get("promoted")).as("re-promote inserts nothing").isEqualTo(0);
        ops.finalizeTenant(T1, false);

        assertThat(count(ctx -> ctx.selectCount().from(CHUNKS).fetchOne(0, Integer.class))).isEqualTo(chunksBefore);
        assertThat(count(ctx -> ctx.selectCount().from(CATALOG_DOCUMENT_CHUNKS).fetchOne(0, Integer.class)))
            .isEqualTo(manifestBefore);
        assertThat(count(ctx -> ctx.selectCount().from(RELEVANCE_LOG).fetchOne(0, Integer.class)))
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
            ctx.insertInto(STAGING_DOCUMENT_CHUNKS, SDC_TENANT_ID, SDC_DOC_ID, SDC_POSITION, SDC_CHASH)
               .values(T1, "1.1.1", 7, ghost)
               .onConflictDoNothing()
               .execute();
            return null;
        });
        Map<String, Object> fin = ops.finalizeTenant(T1, false);
        assertThat(((Number) fin.get("manifest_unresolved")).intValue())
            .as("the ghost pointer is counted unresolved, not promoted")
            .isGreaterThanOrEqualTo(1);
        assertThat(fin.get("dangling_manifest")).isEqualTo(0);
        assertThat(count(ctx -> ctx.selectCount().from(CATALOG_DOCUMENT_CHUNKS)
            .where(CATALOG_DOCUMENT_CHUNKS.CHASH.eq(HexFormat.of().parseHex(ghost)))
            .fetchOne(0, Integer.class)))
            .as("no dangling manifest row was created").isEqualTo(0);
        scope.withTenant(T1, ctx -> {
            ctx.deleteFrom(STAGING_DOCUMENT_CHUNKS).where(SDC_POSITION.eq(7)).execute();
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
            ctx.insertInto(CATALOG_DOCUMENTS, CATALOG_DOCUMENTS.TENANT_ID, CATALOG_DOCUMENTS.TUMBLER,
                           CATALOG_DOCUMENTS.TITLE, CATALOG_DOCUMENTS.CONTENT_TYPE, CATALOG_DOCUMENTS.FILE_PATH,
                           CATALOG_DOCUMENTS.CHUNK_COUNT)
               .values(T1, "5.5.5", "orphaned-note-title", "knowledge", "", 3)
               .onConflict(CATALOG_DOCUMENTS.TENANT_ID, CATALOG_DOCUMENTS.TUMBLER)
               .doNothing()
               .execute();
            ctx.insertInto(STAGING_DOCUMENT_CHUNKS, SDC_TENANT_ID, SDC_DOC_ID, SDC_POSITION, SDC_CHASH)
               .values(T1, "5.5.5", 0, ghost)
               .onConflictDoNothing()
               .execute();
            return null;
        });
        Map<String, Object> fin = ops.finalizeTenant(T1, false);
        assertThat(((Number) fin.get("chunk_count_resynced")).intValue())
            .as("the verbatim-imported count (3) must resync to the promoted rows (0)")
            .isGreaterThanOrEqualTo(1);
        assertThat(count(ctx -> ctx.select(CATALOG_DOCUMENTS.CHUNK_COUNT).from(CATALOG_DOCUMENTS)
            .where(CATALOG_DOCUMENTS.TUMBLER.eq("5.5.5"))
            .fetchOne(0, Integer.class)))
            .as("never trust the verbatim-imported count").isEqualTo(0);
        @SuppressWarnings("unchecked")
        List<String> titles = (List<String>) fin.get("unresolved_knowledge_titles");
        assertThat(titles)
            .as("the store_put-origin doc must be surfaced BY TITLE")
            .contains("orphaned-note-title");
        scope.withTenant(T1, ctx -> {
            ctx.deleteFrom(STAGING_DOCUMENT_CHUNKS).where(SDC_DOC_ID.eq("5.5.5")).execute();
            ctx.deleteFrom(CATALOG_DOCUMENTS).where(CATALOG_DOCUMENTS.TUMBLER.eq("5.5.5")).execute();
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
            ctx.insertInto(CATALOG_DOCUMENTS, CATALOG_DOCUMENTS.TENANT_ID, CATALOG_DOCUMENTS.TUMBLER,
                           CATALOG_DOCUMENTS.TITLE, CATALOG_DOCUMENTS.PHYSICAL_COLLECTION)
               .values(T1, "1.1.1", "promote-doc", COLL_A)
               .onConflict(CATALOG_DOCUMENTS.TENANT_ID, CATALOG_DOCUMENTS.TUMBLER)
               .doNothing()
               .execute();
            return null;
        });
        // RDR-191 Phase 5 (nexus-o8dil.29): fk_catalog_chunks_chunk now requires
        // a matching nexus.chunks row -- a genuinely-dangling row is exactly this
        // test's SUBJECT, so bypass the FK locally: drop the constraint, insert,
        // then re-add it NOT VALID (catalog-029-0's exact shape) so it is live
        // again (unvalidated) afterward.
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            PgContainerHelper.dropConstraint(su, CATALOG_DOCUMENT_CHUNKS, "fk_catalog_chunks_chunk");
            DSL.using(su, SQLDialect.POSTGRES)
               .insertInto(CATALOG_DOCUMENT_CHUNKS, CATALOG_DOCUMENT_CHUNKS.TENANT_ID, CATALOG_DOCUMENT_CHUNKS.DOC_ID,
                           CATALOG_DOCUMENT_CHUNKS.POSITION, CATALOG_DOCUMENT_CHUNKS.CHASH,
                           CATALOG_DOCUMENT_CHUNKS.COLLECTION)
               .values(T1, "1.1.1", 88, HexFormat.of().parseHex(ghost), COLL_A)
               .execute();
            PgContainerHelper.addFkNotValidComposite3(su, CATALOG_DOCUMENT_CHUNKS, "fk_catalog_chunks_chunk",
                "collection", "chash", CHUNKS, "collection", "chash",
                "ON UPDATE CASCADE DEFERRABLE INITIALLY IMMEDIATE");
        }
        try {
            org.assertj.core.api.Assertions.assertThatThrownBy(() -> ops.finalizeTenant(T1, false))
                .isInstanceOf(IllegalStateException.class)
                .hasMessageContaining("dangling manifest");
        } finally {
            try (Connection su = pg.createConnection("")) {
                su.setAutoCommit(true);
                DSL.using(su, SQLDialect.POSTGRES)
                   .deleteFrom(CATALOG_DOCUMENT_CHUNKS).where(CATALOG_DOCUMENT_CHUNKS.POSITION.eq(88)).execute();
            }
        }
        // And the census backstop sees the same class independently.
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            PgContainerHelper.dropConstraint(su, CATALOG_DOCUMENT_CHUNKS, "fk_catalog_chunks_chunk");
            DSL.using(su, SQLDialect.POSTGRES)
               .insertInto(CATALOG_DOCUMENT_CHUNKS, CATALOG_DOCUMENT_CHUNKS.TENANT_ID, CATALOG_DOCUMENT_CHUNKS.DOC_ID,
                           CATALOG_DOCUMENT_CHUNKS.POSITION, CATALOG_DOCUMENT_CHUNKS.CHASH,
                           CATALOG_DOCUMENT_CHUNKS.COLLECTION)
               .values(T1, "1.1.1", 89, HexFormat.of().parseHex(ghost), COLL_A)
               .execute();
            PgContainerHelper.addFkNotValidComposite3(su, CATALOG_DOCUMENT_CHUNKS, "fk_catalog_chunks_chunk",
                "collection", "chash", CHUNKS, "collection", "chash",
                "ON UPDATE CASCADE DEFERRABLE INITIALLY IMMEDIATE");
        }
        try {
            Map<String, Integer> residue = scope.withTenant(T1, ctx ->
                dev.nexus.service.db.ChashCensus.scan(ctx));
            assertThat(residue).containsKey("dangling.catalog_document_chunks");
        } finally {
            try (Connection su = pg.createConnection("")) {
                su.setAutoCommit(true);
                DSL.using(su, SQLDialect.POSTGRES)
                   .deleteFrom(CATALOG_DOCUMENT_CHUNKS).where(CATALOG_DOCUMENT_CHUNKS.POSITION.eq(89)).execute();
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
        // nexus.census_canary: CREATEd ad hoc by this test method (immediately below)
        // and DROPped again below; it is not part of the real product schema and so,
        // like the staging.* tables, carries no generated jOOQ Table -- built via
        // jOOQ's typed CREATE TABLE/GRANT/DROP TABLE DDL API instead (nexus-cbo4a
        // batch 11), never a raw SQL string.
        Table<?> censusCanary = DSL.table(DSL.name("nexus", "census_canary"));
        Field<String> ccTenantId = DSL.field(DSL.name("tenant_id"), String.class);
        Field<String> ccMysteryRef = DSL.field(DSL.name("mystery_ref"), String.class);
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext suCtx = DSL.using(su, SQLDialect.POSTGRES);
            suCtx.createTable(censusCanary)
                 .column(ccTenantId, SQLDataType.CLOB.nullable(false).defaultValue(""))
                 .column(ccMysteryRef, SQLDataType.CLOB)
                 .execute();
            suCtx.grant(DSL.privilege("SELECT")).on(censusCanary).to(DSL.role(SVC_ROLE)).execute();
            suCtx.insertInto(censusCanary, ccTenantId, ccMysteryRef)
                 .values(T1, "0123456789abcdef0123456789abcdef")
                 .execute();
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
                DSL.using(su, SQLDialect.POSTGRES).dropTable(censusCanary).execute();
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
            ctx.insertInto(STAGING_FRECENCY, SF_TENANT_ID, SF_CHUNK_ID, SF_FRECENCY_SCORE)
               .values(T1, lateCanon, 3.25)
               .onConflictDoNothing()
               .execute();
            return null;
        });

        ops.promoteCollection(T1, COLL_LATE, 768);
        Map<String, Object> fin = ops.finalizeTenant(T1, false);

        assertThat(count(ctx -> ctx.selectCount().from(CHUNKS)
            .where(CHUNKS.CHASH.eq(HexFormat.of().parseHex(lateCanon)))
            .fetchOne(0, Integer.class))).isEqualTo(1);
        assertThat(count(ctx -> ctx.selectCount().from(FRECENCY)
            .where(FRECENCY.CHUNK_ID.eq(lateCanon))
            .fetchOne(0, Integer.class)))
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

    // nexus-kmd5b / RDR-194 P3d: census_seesDanglingPointersAtLegacyWidth
    // (Order 13) DELETED — its subject was seeding a legacy-width value
    // into one of four dangling-pointer legs (chash_index, frecency,
    // relevance_log, topic_assignments) and proving the census's width
    // precondition used to blind itself to exactly that population.
    // Production 2026-07-20 measured the consequence directly (chash_index
    // reported 1 against 292,230 actual orphans) — the finding this test
    // preserved. By RDR-194 P3d that scenario can no longer be CONSTRUCTED
    // at all, on any of the four legs: chash_index's table is DROPPED
    // (RDR-187, nexus-piwya.9); frecency/relevance_log both carry a
    // `chunk_id ~ '^[0-9a-f]{64}$'` CHECK (legacy-001-drop-chash-alias.xml,
    // nexus-lgdel.l1) that rejects a legacy-width INSERT outright; and
    // topic_assignments.doc_id now carries the composite
    // topic_assignments_chunk_fk FK (taxonomy-012-doc-id-chunk-fk.xml,
    // this bead) which rejects any doc_id with no matching nexus.chunks
    // row — a bare legacy-width value has no such row by construction, so
    // the seeding INSERT itself would fail with SQLSTATE 23503 before the
    // census ever ran. Leg C1 (dangling.topic_assignments) is retired
    // outright (ChashCensus.java, D0.10); legs C2/C3 (frecency/
    // relevance_log) survive but can no longer be exercised by THIS
    // test's legacy-width-seeding shape — their structural prevention
    // (the CHECK constraint) is a stronger guarantee than a census leg
    // finding the row after the fact, which is exactly why deleting the
    // seed rather than replacing it is correct here, not a coverage
    // regression.

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
     *  advisory-lock shape on a raw connection (nexus-cbo4a batch 11: the same
     *  {@code DSL.function("pg_advisory_xact_lock"/"hashtext"/"set_config", ...)}
     *  idiom {@code StagingPromoteOps.promoteCollection}/{@code PgContainerHelper
     *  .setTenant} already use for these built-in Postgres functions), for tests
     *  needing manual transaction control. A blocked acquire surfaces as jOOQ's
     *  {@link DataAccessException} wrapping the driver's {@link SQLException}
     *  (unwrap via {@code getCause()}), never a bare {@link SQLException} -- the
     *  same wrapper shape {@code addFkNotValid}'s callers already accept. */
    private static void acquireGateExclusive(Connection conn, String tenant, String collection, int lockTimeoutMs) {
        DSLContext ctx = DSL.using(conn, SQLDialect.POSTGRES);
        ctx.select(DSL.function("set_config", SQLDataType.VARCHAR,
                DSL.val("lock_timeout"), DSL.val(String.valueOf(lockTimeoutMs)), DSL.val(true)))
           .fetch();
        ctx.select(DSL.function("pg_advisory_xact_lock", Object.class,
                DSL.function("hashtext", Integer.class, DSL.val("sweepgate:" + tenant + "/" + collection))))
           .fetch();
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
            ctx.insertInto(CATALOG_DOCUMENTS, CATALOG_DOCUMENTS.TENANT_ID, CATALOG_DOCUMENTS.TUMBLER,
                           CATALOG_DOCUMENTS.TITLE, CATALOG_DOCUMENTS.PHYSICAL_COLLECTION)
               .values(T1, "gate-doc-1", "gate doc", COLL_GATE)
               .onConflict(CATALOG_DOCUMENTS.TENANT_ID, CATALOG_DOCUMENTS.TUMBLER)
               .doNothing()
               .execute();
            ctx.insertInto(STAGING_DOCUMENT_CHUNKS, SDC_TENANT_ID, SDC_DOC_ID, SDC_POSITION, SDC_CHASH)
               .values(T1, "gate-doc-1", 0, canonical)
               .onConflictDoNothing()
               .execute();
            return null;
        });

        try (Connection external = dsConnection()) {
            external.setAutoCommit(false);
            PgContainerHelper.setTenant(external, TenantScope.DEFAULT_TENANT_GUC, T1, false);
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

        assertThat(count(ctx -> ctx.selectCount().from(CATALOG_DOCUMENT_CHUNKS)
            .where(CATALOG_DOCUMENT_CHUNKS.DOC_ID.eq("gate-doc-1"))
            .and(CATALOG_DOCUMENT_CHUNKS.CHASH.eq(HexFormat.of().parseHex(canonical)))
            .fetchOne(0, Integer.class)))
            .as("the manifest row landed once the gate was released").isEqualTo(1);
    }

    // ── nexus-11gh6 round 3 (T2 nexus/critique-11gh6-gate-impl-2026-08-08
    //    [21798] REWORK DELTA Critical finding): promoteCollection's OWN
    //    content-insert into chunks_<dim> was left ungated in round 2 on an
    //    incomplete exemption argument -- it must take the gate too. ──

    private static final String COLL_GATE2 = "knowledge__kgate2__bge-base-en-v15-768__v1";

    /** Hand-drives {@code CatalogRepository.acquireSweepGateShared}'s exact
     *  advisory-lock shape on a raw connection (nexus-cbo4a batch 11 -- same
     *  {@code DSL.function} idiom as {@link #acquireGateExclusive}), for tests
     *  needing manual transaction control. */
    private static void acquireGateShared(Connection conn, String tenant, String collection) {
        DSL.using(conn, SQLDialect.POSTGRES)
           .select(DSL.function("pg_advisory_xact_lock_shared", Object.class,
               DSL.function("hashtext", Integer.class, DSL.val("sweepgate:" + tenant + "/" + collection))))
           .fetch();
    }

    @Test
    @Order(21)
    void promoteCollection_contentInsert_blocksOnExternalExclusiveGate_thenProceeds() throws Exception {
        String text = "promote-gate-block content " + System.nanoTime();
        landChunk(COLL_GATE2, 768, digestHex(text), text, vec(768));

        try (Connection external = dsConnection()) {
            external.setAutoCommit(false);
            PgContainerHelper.setTenant(external, TenantScope.DEFAULT_TENANT_GUC, T1, false);
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
            PgContainerHelper.setTenant(connA, TenantScope.DEFAULT_TENANT_GUC, T1, false);
            acquireGateShared(connA, T1, col);
            PgContainerHelper.insertCollection(DSL.using(connA, SQLDialect.POSTGRES), T1, col);
            DSL.using(connA, SQLDialect.POSTGRES)
               .insertInto(CHUNKS, CHUNKS.TENANT_ID, CHUNKS.COLLECTION, CHUNKS.CHASH, CHUNKS.CHUNK_TEXT,
                           CHUNKS.EMBEDDING_768)
               .values(T1, col, HexFormat.of().parseHex(chash), text, zeroVector(768))
               .execute();

            // Connection B: an unrelated document's ordinary sweep for the SAME
            // collection -- short lock_timeout, must be refused while A holds
            // SHARED, so its guarded DELETE never even runs.
            PgContainerHelper.setTenant(connB, TenantScope.DEFAULT_TENANT_GUC, T1, false);
            assertThatThrownBy(() -> acquireGateExclusive(connB, T1, col, 1000))
                .as("a concurrent unrelated document's sweep must be refused the gate while "
                    + "promoteCollection's content-insert transaction holds it SHARED")
                .isInstanceOf(DataAccessException.class)
                .satisfies(e -> assertThat(((SQLException) e.getCause()).getSQLState()).isEqualTo("55P03"));
            connB.rollback();

            connA.commit();
        }

        assertThat(count(ctx -> ctx.selectCount().from(CHUNKS)
            .where(CHUNKS.COLLECTION.eq(col))
            .and(CHUNKS.CHASH.eq(HexFormat.of().parseHex(chash)))
            .fetchOne(0, Integer.class)))
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
            ctx.insertInto(CATALOG_DOCUMENTS, CATALOG_DOCUMENTS.TENANT_ID, CATALOG_DOCUMENTS.TUMBLER,
                           CATALOG_DOCUMENTS.TITLE, CATALOG_DOCUMENTS.PHYSICAL_COLLECTION)
               .values(T1, "multi-doc-a", "multi doc a", colA)
               .onConflict(CATALOG_DOCUMENTS.TENANT_ID, CATALOG_DOCUMENTS.TUMBLER)
               .doNothing()
               .execute();
            ctx.insertInto(CATALOG_DOCUMENTS, CATALOG_DOCUMENTS.TENANT_ID, CATALOG_DOCUMENTS.TUMBLER,
                           CATALOG_DOCUMENTS.TITLE, CATALOG_DOCUMENTS.PHYSICAL_COLLECTION)
               .values(T1, "multi-doc-b", "multi doc b", colB)
               .onConflict(CATALOG_DOCUMENTS.TENANT_ID, CATALOG_DOCUMENTS.TUMBLER)
               .doNothing()
               .execute();
            ctx.insertInto(STAGING_DOCUMENT_CHUNKS, SDC_TENANT_ID, SDC_DOC_ID, SDC_POSITION, SDC_CHASH)
               .values(T1, "multi-doc-a", 0, chashA)
               .onConflictDoNothing()
               .execute();
            ctx.insertInto(STAGING_DOCUMENT_CHUNKS, SDC_TENANT_ID, SDC_DOC_ID, SDC_POSITION, SDC_CHASH)
               .values(T1, "multi-doc-b", 0, chashB)
               .onConflictDoNothing()
               .execute();
            return null;
        });

        // Hold colB's gate EXCLUSIVE externally. If the resolution loop only
        // gated ONE collection (e.g. the first resolved, or a hardcoded one),
        // this call would NOT block.
        try (Connection external = dsConnection()) {
            external.setAutoCommit(false);
            PgContainerHelper.setTenant(external, TenantScope.DEFAULT_TENANT_GUC, T1, false);
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

        assertThat(count(ctx -> ctx.selectCount().from(CATALOG_DOCUMENT_CHUNKS)
            .where(CATALOG_DOCUMENT_CHUNKS.DOC_ID.eq("multi-doc-a"))
            .and(CATALOG_DOCUMENT_CHUNKS.CHASH.eq(HexFormat.of().parseHex(chashA)))
            .fetchOne(0, Integer.class))).isEqualTo(1);
        assertThat(count(ctx -> ctx.selectCount().from(CATALOG_DOCUMENT_CHUNKS)
            .where(CATALOG_DOCUMENT_CHUNKS.DOC_ID.eq("multi-doc-b"))
            .and(CATALOG_DOCUMENT_CHUNKS.CHASH.eq(HexFormat.of().parseHex(chashB)))
            .fetchOne(0, Integer.class))).isEqualTo(1);
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
        assertThat(count(ctx -> ctx.selectCount().from(CHUNKS)
            .where(CHUNKS.COLLECTION.eq(COLL_F12B))
            .and(CHUNKS.CHASH.eq(HexFormat.of().parseHex(canonical)))
            .fetchOne(0, Integer.class)))
            .as("regression pin: the CONTENT insert (StagingPromoteOps :410-435) "
                + "must be left untouched by this fix — it already stamps collection")
            .isEqualTo(1);

        scope.withTenant(T1, ctx -> {
            ctx.insertInto(CATALOG_DOCUMENTS, CATALOG_DOCUMENTS.TENANT_ID, CATALOG_DOCUMENTS.TUMBLER,
                           CATALOG_DOCUMENTS.TITLE, CATALOG_DOCUMENTS.PHYSICAL_COLLECTION)
               .values(T1, "f12b-doc", "f12b doc", COLL_F12B)
               .onConflict(CATALOG_DOCUMENTS.TENANT_ID, CATALOG_DOCUMENTS.TUMBLER)
               .doNothing()
               .execute();
            ctx.insertInto(STAGING_DOCUMENT_CHUNKS, SDC_TENANT_ID, SDC_DOC_ID, SDC_POSITION, SDC_CHASH)
               .values(T1, "f12b-doc", 0, canonical)
               .onConflictDoNothing()
               .execute();
            return null;
        });

        Map<String, Object> fin = ops.finalizeTenant(T1, false);
        assertThat(fin.get("manifest_promoted")).isEqualTo(1);

        assertThat(count(ctx -> ctx.selectCount().from(CATALOG_DOCUMENT_CHUNKS)
            .where(CATALOG_DOCUMENT_CHUNKS.DOC_ID.eq("f12b-doc"))
            .and(CATALOG_DOCUMENT_CHUNKS.CHASH.eq(HexFormat.of().parseHex(canonical)))
            .and(CATALOG_DOCUMENT_CHUNKS.COLLECTION.eq(COLL_F12B))
            .fetchOne(0, Integer.class)))
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
            ctx.insertInto(STAGING_DOCUMENT_CHUNKS, SDC_TENANT_ID, SDC_DOC_ID, SDC_POSITION, SDC_CHASH)
               .values(T1, doc, 0, chash)
               .onConflictDoNothing()
               .execute();
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
        assertThat(count(ctx -> ctx.selectCount().from(CATALOG_DOCUMENT_CHUNKS)
            .where(CATALOG_DOCUMENT_CHUNKS.DOC_ID.eq(doc))
            .fetchOne(0, Integer.class)))
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
            ctx.deleteFrom(STAGING_DOCUMENT_CHUNKS)
               .where(SDC_TENANT_ID.eq(T1)).and(SDC_DOC_ID.eq(doc))
               .execute();
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
            ctx.insertInto(CATALOG_DOCUMENTS, CATALOG_DOCUMENTS.TENANT_ID, CATALOG_DOCUMENTS.TUMBLER,
                           CATALOG_DOCUMENTS.TITLE, CATALOG_DOCUMENTS.PHYSICAL_COLLECTION)
               .values(T1, doc, "g7 doc", COLL_G6)
               .onConflict(CATALOG_DOCUMENTS.TENANT_ID, CATALOG_DOCUMENTS.TUMBLER)
               .doNothing()
               .execute();
            return null;
        });

        landChunk(COLL_G6, 768, newCanonical, newText, vec(768));
        assertThat(ops.promoteCollection(T1, COLL_G6, 768).get("promoted")).isEqualTo(1);

        scope.withTenant(T1, ctx -> {
            ctx.insertInto(STAGING_DOCUMENT_CHUNKS, SDC_TENANT_ID, SDC_DOC_ID, SDC_POSITION, SDC_CHASH)
               .values(T1, doc, 0, newCanonical)
               .onConflictDoNothing()
               .execute();
            return null;
        });

        ops.finalizeTenant(T1, false);
        assertThat(count(ctx -> ctx.selectCount().from(CATALOG_DOCUMENT_CHUNKS)
            .where(CATALOG_DOCUMENT_CHUNKS.DOC_ID.eq(doc))
            .and(CATALOG_DOCUMENT_CHUNKS.CHASH.eq(HexFormat.of().parseHex(newCanonical)))
            .and(CATALOG_DOCUMENT_CHUNKS.COLLECTION.eq(COLL_G6))
            .fetchOne(0, Integer.class)))
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
            ctx.insertInto(CATALOG_DOCUMENTS, CATALOG_DOCUMENTS.TENANT_ID, CATALOG_DOCUMENTS.TUMBLER,
                           CATALOG_DOCUMENTS.TITLE, CATALOG_DOCUMENTS.PHYSICAL_COLLECTION)
               .values(T1, doc, "g8 ghost doc", "")
               .onConflict(CATALOG_DOCUMENTS.TENANT_ID, CATALOG_DOCUMENTS.TUMBLER)
               .doNothing()
               .execute();
            return null;
        });

        // The chash is genuinely resolvable -- real content, landed and
        // promoted -- so a resolvability check alone would not block it.
        landChunk(collZ, 768, canonical, text, vec(768));
        assertThat(ops.promoteCollection(T1, collZ, 768).get("promoted")).isEqualTo(1);

        scope.withTenant(T1, ctx -> {
            ctx.insertInto(STAGING_DOCUMENT_CHUNKS, SDC_TENANT_ID, SDC_DOC_ID, SDC_POSITION, SDC_CHASH)
               .values(T1, doc, 1, canonical)
               .onConflictDoNothing()
               .execute();
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

        assertThat(count(ctx -> ctx.selectCount().from(CATALOG_DOCUMENT_CHUNKS)
            .where(CATALOG_DOCUMENT_CHUNKS.DOC_ID.eq(doc))
            .and(CATALOG_DOCUMENT_CHUNKS.CHASH.eq(HexFormat.of().parseHex(canonical)))
            .fetchOne(0, Integer.class)))
            .as("RDR-191: an empty physical_collection blocks the manifest "
                + "row even though the chash is genuinely resolvable -- "
                + "resolvability was never the gate")
            .isEqualTo(0);
        assertThat(count(ctx -> ctx.selectCount().from(STAGING_DOCUMENT_CHUNKS)
            .where(SDC_TENANT_ID.eq(T1)).and(SDC_DOC_ID.eq(doc)).and(SDC_POSITION.eq(1))
            .fetchOne(0, Integer.class)))
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
            ctx.deleteFrom(STAGING_DOCUMENT_CHUNKS)
               .where(SDC_TENANT_ID.eq(T1)).and(SDC_DOC_ID.eq(doc))
               .execute();
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
            ctx.insertInto(CATALOG_DOCUMENTS, CATALOG_DOCUMENTS.TENANT_ID, CATALOG_DOCUMENTS.TUMBLER,
                           CATALOG_DOCUMENTS.TITLE, CATALOG_DOCUMENTS.PHYSICAL_COLLECTION)
               .values(T_DIM, doc, "dim-check doc", coll)
               .onConflict(CATALOG_DOCUMENTS.TENANT_ID, CATALOG_DOCUMENTS.TUMBLER)
               .doNothing()
               .execute();
            // chunks_<dim> FK-references catalog_collections(tenant_id, name)
            // (fk-002-collection-registry.xml) — the orphan content INSERT
            // below needs the stub row promoteCollection's own step (4)
            // would normally create; this collection is never promoted
            // through that path so it must be stubbed directly here.
            PgContainerHelper.insertCollection(ctx, T_DIM, coll);
            return null;
        });
        // The orphan itself: an empty-text staged chunk row at this dim —
        // orphanCond requires no non-empty sibling row sharing the ref
        // (fresh ref here, so it holds).
        scope.withTenant(T_DIM, ctx -> {
            ctx.insertInto(STAGING_CHUNKS, SC_TENANT_ID, SC_COLLECTION, SC_DIM, SC_LEGACY_REF, SC_CHUNK_TEXT,
                           SC_EMBEDDING, SC_MODEL)
               .values(T_DIM, coll, dim, legacyRef, "", zeroVector(dim), "model-" + dim)
               .onConflict(SC_TENANT_ID, SC_COLLECTION, SC_LEGACY_REF)
               .doNothing()
               .execute();
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
        assertThat(countAs(T_DIM, ctx -> ctx.selectCount().from(CHUNKS)
            .where(CHUNKS.TENANT_ID.eq(T_DIM))
            .and(CHUNKS.CHASH.eq(HexFormat.of().parseHex(synthChashHex)))
            .and(CHUNKS.CHUNK_TEXT.eq(""))
            .and(DSL.jsonbGetAttributeAsText(CHUNKS.METADATA, "chash_origin").eq("synthetic"))
            .and(embeddingColumn(dim).isNotNull())
            .fetchOne(0, Integer.class)))
            .as("dim " + dim + ": the surrogate content row landed with the "
                + "synthetic stamp AND its vector in the dim-correct column")
            .isEqualTo(1);
        for (int other : new int[] {384, 768, 1024}) {
            if (other == dim) continue;
            assertThat(countAs(T_DIM, ctx -> ctx.selectCount().from(CHUNKS)
                .where(CHUNKS.TENANT_ID.eq(T_DIM))
                .and(CHUNKS.CHASH.eq(HexFormat.of().parseHex(synthChashHex)))
                .and(embeddingColumn(other).isNotNull())
                .fetchOne(0, Integer.class)))
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
        //
        // hygiene-001 step 1 (nexus-tk070.p6a follow-on): nexus.document_aspects
        // .source_uri is NOT NULL now too. StagingPromoteOps' promote SELECT
        // carries staging.document_aspects.source_uri straight through with the
        // same no-normalization behavior as doc_id above (StagingPromoteOps.java
        // ~L1148: "source_path/source_uri carry no in-flight rewrite here") --
        // this is the identical pre-existing fixture gap, not an engine defect;
        // supply a real source_uri here rather than leaving the staged column
        // (nullable, no default) unset.
        String doc = "aspects-promote-json-doc";
        scope.withTenant(T1, ctx -> {
            PgContainerHelper.insertCollection(ctx, T1, ASPECTS_PROMOTE_COLL);
            ctx.insertInto(CATALOG_DOCUMENTS, CATALOG_DOCUMENTS.TENANT_ID, CATALOG_DOCUMENTS.TUMBLER,
                           CATALOG_DOCUMENTS.TITLE)
               .values(T1, doc, "aspects-promote-json fixture")
               .onConflict(CATALOG_DOCUMENTS.TENANT_ID, CATALOG_DOCUMENTS.TUMBLER)
               .doNothing()
               .execute();
            ctx.insertInto(STAGING_DOCUMENT_ASPECTS, SDA_TENANT_ID, SDA_DOC_ID, SDA_COLLECTION, SDA_SOURCE_PATH,
                           SDA_EXTRACTED_AT, SDA_MODEL_VERSION, SDA_EXTRACTOR_NAME, SDA_SOURCE_URI, SDA_EXTRAS)
               .values(T1, doc, ASPECTS_PROMOTE_COLL, "aspects-promote-json.pdf", "", "v1", "ex",
                       "file:///aspects-promote-json.pdf", "{\"venue\": \"VLDB\", \"year\": \"2023\"}")
               .execute();
            return null;
        });

        Map<String, Object> fin = ops.finalizeTenant(T1, false);
        assertThat(fin.get("document_aspects_promoted"))
            .as("the staged document_aspects row must promote (anti-join sees a new row)")
            .isEqualTo(1);

        JSONB extrasJsonb = scope.withTenant(T1, ctx -> ctx.select(DOCUMENT_ASPECTS.EXTRAS)
            .from(DOCUMENT_ASPECTS)
            .where(DOCUMENT_ASPECTS.TENANT_ID.eq(T1))
            .and(DOCUMENT_ASPECTS.COLLECTION.eq(ASPECTS_PROMOTE_COLL))
            .and(DOCUMENT_ASPECTS.SOURCE_PATH.eq("aspects-promote-json.pdf"))
            .fetchOne(DOCUMENT_ASPECTS.EXTRAS));
        String extrasText = extrasJsonb.data();
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
            PgContainerHelper.insertCollection(ctx, T1, coll);
            ctx.insertInto(CATALOG_DOCUMENTS, CATALOG_DOCUMENTS.TENANT_ID, CATALOG_DOCUMENTS.TUMBLER,
                           CATALOG_DOCUMENTS.TITLE)
               .values(T1, doc, "aspects-promote-malformed fixture")
               .onConflict(CATALOG_DOCUMENTS.TENANT_ID, CATALOG_DOCUMENTS.TUMBLER)
               .doNothing()
               .execute();
            ctx.insertInto(STAGING_DOCUMENT_ASPECTS, SDA_TENANT_ID, SDA_DOC_ID, SDA_COLLECTION, SDA_SOURCE_PATH,
                           SDA_EXTRACTED_AT, SDA_MODEL_VERSION, SDA_EXTRACTOR_NAME, SDA_EXTRAS)
               .values(T1, doc, coll, "aspects-promote-malformed.pdf", "", "v1", "ex", "not-json-at-all")
               .execute();
            return null;
        });

        assertThatThrownBy(() -> ops.finalizeTenant(T1, false))
            .as("malformed staged extras must fail the promote loud, not land silently")
            .isInstanceOf(RuntimeException.class)
            .hasMessageContaining("json");

        int landed = count(ctx -> ctx.selectCount().from(DOCUMENT_ASPECTS)
            .where(DOCUMENT_ASPECTS.COLLECTION.eq(coll))
            .and(DOCUMENT_ASPECTS.SOURCE_PATH.eq("aspects-promote-malformed.pdf"))
            .fetchOne(0, Integer.class));
        assertThat(landed).as("the malformed row must NOT have landed in nexus.document_aspects")
            .isEqualTo(0);

        // Cleanup: T1's ordered sequence ends here, but stay consistent with
        // this file's own convention (Order 3/4) of cleaning up a fail-loud
        // scenario's staged row so no later finalize run would keep re-attempting
        // (and re-failing on) it.
        scope.withTenant(T1, ctx -> {
            ctx.deleteFrom(STAGING_DOCUMENT_ASPECTS).where(SDA_COLLECTION.eq(coll)).execute();
            return null;
        });
    }

    // ── Order 34: RDR-194 P3d (nexus-tk070.p3d), non-conformant doc_id ──────
    //    skip-and-scream (nexus-cncue DISPOSITION OF RECORD, T2 nexus/cncue-
    //    finalize-reject-reconciliation [22750]) ────────────────────────────

    @Test
    @Order(34)
    void finalizeTenant_nonConformantTopicAssignmentDocId_excludedByCount() {
        // A staged doc_id that is not already a conformant 64-hex chash is
        // arbitrary text: a memory-note title, a historic tumbler, a
        // legacy 16/32-hex shape, anything an ETL-era row could have
        // carried. RDR-194 P3d converts the former IllegalStateException
        // hard-throw (nexus-tk070.p3a) to a counted + WARN-logged
        // exclusion (nexus-cncue's disposition, option (b)) — the promote
        // INSERT's own `staDocId.likeRegex('^[0-9a-f]{64}$')` WHERE clause
        // already guarantees the value can never reach
        // nexus.topic_assignments, so this is a visibility-only change,
        // not a write-safety one.
        String badDocId = "some-memory-note-title-not-a-chash";
        scope.withTenant(T_REJECT, ctx -> {
            PgContainerHelper.insertCollection(ctx, T_REJECT, COLL_A);
            ctx.insertInto(STAGING_TOPIC_ASSIGNMENTS, STA_TENANT_ID, STA_DOC_ID, STA_TOPIC_ID, STA_TOPIC_LABEL,
                           STA_TOPIC_COLLECTION)
               .values(T_REJECT, badDocId, 999999L, "reject-topic", COLL_A)
               .onConflictDoNothing()
               .execute();
            return null;
        });

        Map<String, Object> fin = ops.finalizeTenant(T_REJECT, false);

        assertThat(((Number) fin.get("topic_assignments_non_conformant")).intValue())
            .as("the FALSIFIABLE half of this test: delete the counting code "
                + "and this goes to zero — a bare row-count-stays-zero "
                + "assertion alone would pass identically whether or not the "
                + "exclusion logic runs at all (cncue analysis's own vacuity "
                + "finding against the prior throw-based version of this test)")
            .isEqualTo(1);
        assertThat(countAs(T_REJECT, ctx -> ctx.selectCount().from(TOPIC_ASSIGNMENTS)
            .where(TOPIC_ASSIGNMENTS.TENANT_ID.eq(T_REJECT))
            .fetchOne(0, Integer.class)))
            .as("the excluded row must never reach nexus.topic_assignments")
            .isEqualTo(0);
        // Re-running finalize over the same excluded row must not throw —
        // this method is explicitly idempotent/re-runnable (class javadoc),
        // and the whole point of skip-and-scream is that it never wedges.
        ops.finalizeTenant(T_REJECT, false);

        // Cleanup (Order 33's convention): the row is harmless now (no
        // wedge), but leaving it staged forever would pollute the counts
        // map for the sibling @Order tests below that share this tenant.
        scope.withTenant(T_REJECT, ctx -> {
            ctx.deleteFrom(STAGING_TOPIC_ASSIGNMENTS).where(STA_DOC_ID.eq(badDocId)).execute();
            return null;
        });
    }

    // ── Order 35: the positive control cncue's analysis flagged as MISSING ──

    @Test
    @Order(35)
    void finalizeTenant_nonConformantAlongsideResolvable_conformantStillPromotes() {
        // The single assertion that actually falsifies the wedge (cncue
        // analysis §4 item 4, "the non-vacuous gate for this whole
        // discussion"): a non-conformant row sharing a finalize transaction
        // with a fully resolvable, conformant row must not block the
        // resolvable row from promoting. The prior hard-throw made this
        // scenario untestable — the transaction aborted before either row's
        // fate could be observed. Nothing in the suite tested it before.
        String badDocId = "another-non-conformant-title";
        String goodText = "resolvable topic assignment content " + System.nanoTime();
        String goodChash = digestHex(goodText);
        scope.withTenant(T_REJECT, ctx -> {
            PgContainerHelper.insertCollection(ctx, T_REJECT, COLL_A);
            // No onConflict target here (the original bare "ON CONFLICT DO NOTHING" named
            // none either): topics.id is an unsupplied identity column, so a fresh insert
            // always gets a fresh id and can never collide with the table's own UNIQUE
            // (tenant_id, id) -- the clause was already inert protection against a
            // collision this insert shape cannot produce.
            ctx.insertInto(TOPICS, TOPICS.TENANT_ID, TOPICS.LABEL, TOPICS.COLLECTION, TOPICS.CREATED_AT)
               .values(T_REJECT, "reject-topic-resolvable", COLL_A, OffsetDateTime.now())
               .execute();
            ctx.insertInto(STAGING_CHUNKS, SC_TENANT_ID, SC_COLLECTION, SC_DIM, SC_LEGACY_REF, SC_CHUNK_TEXT,
                           SC_EMBEDDING, SC_MODEL)
               .values(T_REJECT, COLL_A, 768, goodChash, goodText, zeroVector(768), "bge-768")
               .onConflict(SC_TENANT_ID, SC_COLLECTION, SC_LEGACY_REF)
               .doUpdate()
               .set(SC_CHUNK_TEXT, DSL.excluded(SC_CHUNK_TEXT))
               .execute();
            ctx.insertInto(STAGING_TOPIC_ASSIGNMENTS, STA_TENANT_ID, STA_DOC_ID, STA_TOPIC_ID, STA_TOPIC_LABEL,
                           STA_TOPIC_COLLECTION)
               .values(T_REJECT, badDocId, 999999L, "reject-topic-resolvable", COLL_A)
               .onConflictDoNothing()
               .execute();
            ctx.insertInto(STAGING_TOPIC_ASSIGNMENTS, STA_TENANT_ID, STA_DOC_ID, STA_TOPIC_ID, STA_TOPIC_LABEL,
                           STA_TOPIC_COLLECTION)
               .values(T_REJECT, goodChash, 999999L, "reject-topic-resolvable", COLL_A)
               .onConflictDoNothing()
               .execute();
            return null;
        });
        ops.promoteCollection(T_REJECT, COLL_A, 768);

        Map<String, Object> fin = ops.finalizeTenant(T_REJECT, false);

        assertThat(((Number) fin.get("topic_assignments_non_conformant")).intValue())
            .as("the non-conformant row is still excluded and counted")
            .isEqualTo(1);
        assertThat(countAs(T_REJECT, ctx -> ctx.selectCount().from(TOPIC_ASSIGNMENTS)
            .where(TOPIC_ASSIGNMENTS.TENANT_ID.eq(T_REJECT))
            .and(TOPIC_ASSIGNMENTS.DOC_ID.eq(HexFormat.of().parseHex(goodChash)))
            .fetchOne(0, Integer.class)))
            .as("the CONFORMANT, fully-resolvable row must promote in the SAME "
                + "finalize call despite the non-conformant row's presence in "
                + "the same staged batch")
            .isEqualTo(1);

        scope.withTenant(T_REJECT, ctx -> {
            ctx.deleteFrom(STAGING_TOPIC_ASSIGNMENTS).where(STA_DOC_ID.in(badDocId, goodChash)).execute();
            return null;
        });
    }

    // ── Order 36: RESOLVABLE-ONLY anti-join — the scope-enlargement fix ──────

    @Test
    @Order(36)
    void finalizeTenant_conformantDocIdNoMatchingChunk_staysStagedNeverHitsFk() {
        // RDR-194 P3d scope enlargement (cncue analysis §2b/§4, CRITICAL
        // finding): a CONFORMANT 64-hex staged doc_id whose content
        // collection has not promoted to nexus.chunks yet must stay
        // STAGED, counted — never reach the topic_assignments_chunk_fk
        // composite FK, which would otherwise abort the WHOLE tenant
        // finalize with SQLSTATE 23503 naming no offending row. This is
        // exactly the RESOLVABLE-ONLY deferral this method's own comment
        // promises ("a later finalize converges it once its content
        // collection promotes"). Falsify by deleting the
        // topicChunkResolvable anti-join in StagingPromoteOps — this test
        // must go red with an unchecked FK-violation exception, not a
        // graceful count.
        String pendingChash = digestHex("chunk that has not promoted yet " + System.nanoTime());
        scope.withTenant(T_REJECT, ctx -> {
            PgContainerHelper.insertCollection(ctx, T_REJECT, COLL_A);
            // No onConflict target (see the identical topics insert above): id is an
            // unsupplied identity column, so a fresh row can never collide.
            ctx.insertInto(TOPICS, TOPICS.TENANT_ID, TOPICS.LABEL, TOPICS.COLLECTION, TOPICS.CREATED_AT)
               .values(T_REJECT, "reject-topic-pending", COLL_A, OffsetDateTime.now())
               .execute();
            ctx.insertInto(STAGING_TOPIC_ASSIGNMENTS, STA_TENANT_ID, STA_DOC_ID, STA_TOPIC_ID, STA_TOPIC_LABEL,
                           STA_TOPIC_COLLECTION)
               .values(T_REJECT, pendingChash, 999999L, "reject-topic-pending", COLL_A)
               .onConflictDoNothing()
               .execute();
            return null;
        });

        Map<String, Object> fin = ops.finalizeTenant(T_REJECT, false);

        assertThat(((Number) fin.get("topic_assignments_chunk_pending")).intValue())
            .as("the conformant-but-unresolvable row must be counted as "
                + "pending, not silently dropped, not promoted, and not the "
                + "trigger for a tenant-wide FK abort")
            .isEqualTo(1);
        assertThat(countAs(T_REJECT, ctx -> ctx.selectCount().from(TOPIC_ASSIGNMENTS)
            .where(TOPIC_ASSIGNMENTS.TENANT_ID.eq(T_REJECT))
            .and(TOPIC_ASSIGNMENTS.DOC_ID.eq(HexFormat.of().parseHex(pendingChash)))
            .fetchOne(0, Integer.class)))
            .as("the row must stay staged, never promoted")
            .isEqualTo(0);

        scope.withTenant(T_REJECT, ctx -> {
            ctx.deleteFrom(STAGING_TOPIC_ASSIGNMENTS).where(STA_DOC_ID.eq(pendingChash)).execute();
            return null;
        });
    }

    // ── Order 37: the FK itself, independent of the application-layer guard ─

    @Test
    @Order(37)
    void topicAssignmentsChunkFk_rejectsRawInsertForNonexistentChunk() throws Exception {
        // Non-vacuity for the FK itself (RDR-194 P3d acceptance criteria):
        // independent of the application-layer anti-join (Order 36 above),
        // the topic_assignments_chunk_fk constraint must reject a RAW
        // INSERT whose (tenant_id, source_collection, doc_id) has no
        // matching nexus.chunks row — proves the FK is actually VALIDATEd
        // and enforced, not merely NOT VALID or silently absent. Also
        // exercises D1's MATCH SIMPLE non-vacuity claim: source_collection
        // is passed non-NULL here (P3b's SET NOT NULL forbids any row from
        // exempting itself through MATCH SIMPLE's null-exemption anyway).
        String orphanChash = digestHex("never landed anywhere " + System.nanoTime());
        long topicId;
        try (Connection conn = dsConnection()) {
            conn.setAutoCommit(true);
            PgContainerHelper.setTenant(conn, TenantScope.DEFAULT_TENANT_GUC, T_REJECT, false);
            var c = DSL.using(conn, SQLDialect.POSTGRES);
            PgContainerHelper.insertCollection(c, T_REJECT, COLL_A);
            topicId = c.insertInto(TOPICS, TOPICS.TENANT_ID, TOPICS.LABEL, TOPICS.COLLECTION, TOPICS.CREATED_AT)
                       .values(T_REJECT, "reject-topic-fk-raw", COLL_A, OffsetDateTime.now())
                       .returningResult(TOPICS.ID)
                       .fetchOne()
                       .getValue(TOPICS.ID);
            long finalTopicId = topicId;
            // jOOQ wraps the driver's SQLException in its own DataAccessException
            // (nexus-cbo4a batch 11 -- unwrap via getCause() to reach the real
            // java.sql.SQLException/getSQLState(), the same shape every other
            // FK/constraint-violation assertion in this file now uses since the
            // whole insert moved off raw JDBC onto typed DSL). Proving the FK is
            // VALIDATEd via its SQLSTATE is this test's actual subject (see its
            // javadoc).
            assertThatThrownBy(() ->
                    c.insertInto(TOPIC_ASSIGNMENTS, TOPIC_ASSIGNMENTS.TENANT_ID, TOPIC_ASSIGNMENTS.DOC_ID,
                                 TOPIC_ASSIGNMENTS.TOPIC_ID, TOPIC_ASSIGNMENTS.SOURCE_COLLECTION)
                     .values(T_REJECT, HexFormat.of().parseHex(orphanChash), finalTopicId, COLL_A)
                     .execute())
                .as("topic_assignments_chunk_fk must reject an assignment whose "
                    + "(source_collection, doc_id) has no matching nexus.chunks "
                    + "row — proves the FK is VALIDATEd and enforced")
                .isInstanceOf(DataAccessException.class)
                .satisfies(e -> assertThat(((SQLException) e.getCause()).getSQLState()).isEqualTo("23503"));
        }
    }

    // ── Order 38/39 (nexus-eanej): danglingManifestCountDsl re-keyed to
    //    (tenant_id, collection, chash) ────────────────────────────────────
    //
    // fk_catalog_chunks_chunk (catalog-029-manifest-chunk-fk.xml) now
    // VALIDATEs the SAME triple this fix adds to the DSL join, which makes a
    // genuinely-dangling row impossible to COMMIT any more — every write
    // path is already guarded at the database layer. These two tests seed
    // the corruption inside ONE transaction with the FK explicitly deferred
    // (the only way to get such a row to exist at all, even momentarily),
    // assert against that SAME uncommitted connection/DSLContext, then roll
    // back — no dangling row is ever persisted past the test.

    @Test
    @Order(38)
    void danglingManifestCountDsl_chashResolvesOnlyInAnotherCollection_stillCountedDangling() throws Exception {
        // THE 2.2x-undercount shape (2,951 reported vs 6,501 actual, 2026-08-11
        // live census): a manifest row whose chash exists ONLY under a DIFFERENT
        // collection (same tenant) must still be counted dangling. The pre-fix
        // chash-only NOT EXISTS treated a match under ANY collection as
        // resolution — falsify by reverting ChashSqlIdioms.danglingManifestCountDsl's
        // join to CHASH-only and this assertion goes to 0.
        String tenant = "t-eanej-collision";
        String collReal = "knowledge__eanej-real__bge-base-en-v15-768__v1";
        String collDangling = "knowledge__eanej-dangling__bge-base-en-v15-768__v1";
        String text = "eanej cross-collection decoy content " + System.nanoTime();
        byte[] chash = HexFormat.of().parseHex(digestHex(text));

        try (Connection conn = dsConnection()) {
            conn.setAutoCommit(false);
            PgContainerHelper.setTenant(conn, TenantScope.DEFAULT_TENANT_GUC, tenant, false);
            var ctx = DSL.using(conn, SQLDialect.POSTGRES);
            // The ONE sanctioned copy of this statement project-wide (see its
            // javadoc) -- SET CONSTRAINTS has no jOOQ typed-DSL form at all.
            CatalogRepository.deferManifestChunkFk(ctx);

            PgContainerHelper.insertCollection(ctx, tenant, collReal);
            PgContainerHelper.insertCollection(ctx, tenant, collDangling);
            ctx.insertInto(CATALOG_DOCUMENTS, CATALOG_DOCUMENTS.TENANT_ID, CATALOG_DOCUMENTS.TUMBLER,
                           CATALOG_DOCUMENTS.TITLE, CATALOG_DOCUMENTS.PHYSICAL_COLLECTION)
               .values(tenant, "eanej-doc-1", "eanej doc", collDangling)
               .execute();
            // The content lands ONLY in collReal -- the decoy match a chash-only
            // join would have wrongly resolved the collDangling row against.
            ctx.insertInto(CHUNKS, CHUNKS.TENANT_ID, CHUNKS.COLLECTION, CHUNKS.CHASH, CHUNKS.CHUNK_TEXT,
                           CHUNKS.EMBEDDING_768)
               .values(tenant, collReal, chash, text, zeroVector(768))
               .execute();
            // The manifest row names collDangling: no content row exists at
            // (tenant, collDangling, chash) -- FK-legal here ONLY because the
            // constraint was deferred above.
            ctx.insertInto(CATALOG_DOCUMENT_CHUNKS, CATALOG_DOCUMENT_CHUNKS.TENANT_ID,
                           CATALOG_DOCUMENT_CHUNKS.DOC_ID, CATALOG_DOCUMENT_CHUNKS.POSITION,
                           CATALOG_DOCUMENT_CHUNKS.CHASH, CATALOG_DOCUMENT_CHUNKS.COLLECTION)
               .values(tenant, "eanej-doc-1", 0, chash, collDangling)
               .execute();

            assertThat(ChashSqlIdioms.danglingManifestCountDsl(ctx))
                .as("the manifest row's chash resolves only under a DIFFERENT collection -- "
                    + "the pre-fix chash-only join would have read this as resolved (0); the "
                    + "triple-keyed join must still count it as dangling")
                .isEqualTo(1);

            conn.rollback();  // never commit a genuinely-dangling row
        }
    }

    @Test
    @Order(39)
    void danglingManifestCountDsl_noMatchAnywhere_isCountedAndWouldAbortFinalize() throws Exception {
        // The companion in-scope case: a manifest row whose chash resolves
        // NOWHERE at all (no decoy collection, no decoy tenant) must still be
        // counted -- this is the count StagingPromoteOps#finalizeTenant step 7
        // feeds its abort gate with (throws IllegalStateException when nonzero).
        String tenant = "t-eanej-nodecoy";
        String coll = "knowledge__eanej-nodecoy__bge-base-en-v15-768__v1";
        byte[] ghostChash = HexFormat.of().parseHex(
            digestHex("eanej ghost content resolving nowhere " + System.nanoTime()));

        try (Connection conn = dsConnection()) {
            conn.setAutoCommit(false);
            PgContainerHelper.setTenant(conn, TenantScope.DEFAULT_TENANT_GUC, tenant, false);
            var ctx = DSL.using(conn, SQLDialect.POSTGRES);
            // The ONE sanctioned copy of this statement project-wide (see its
            // javadoc) -- SET CONSTRAINTS has no jOOQ typed-DSL form at all.
            CatalogRepository.deferManifestChunkFk(ctx);

            PgContainerHelper.insertCollection(ctx, tenant, coll);
            ctx.insertInto(CATALOG_DOCUMENTS, CATALOG_DOCUMENTS.TENANT_ID, CATALOG_DOCUMENTS.TUMBLER,
                           CATALOG_DOCUMENTS.TITLE, CATALOG_DOCUMENTS.PHYSICAL_COLLECTION)
               .values(tenant, "eanej-doc-2", "eanej doc 2", coll)
               .execute();
            ctx.insertInto(CATALOG_DOCUMENT_CHUNKS, CATALOG_DOCUMENT_CHUNKS.TENANT_ID,
                           CATALOG_DOCUMENT_CHUNKS.DOC_ID, CATALOG_DOCUMENT_CHUNKS.POSITION,
                           CATALOG_DOCUMENT_CHUNKS.CHASH, CATALOG_DOCUMENT_CHUNKS.COLLECTION)
               .values(tenant, "eanej-doc-2", 0, ghostChash, coll)
               .execute();

            assertThat(ChashSqlIdioms.danglingManifestCountDsl(ctx))
                .as("no content row exists anywhere for this chash -- must be counted, "
                    + "the same signal finalizeTenant's abort gate consumes")
                .isEqualTo(1);

            conn.rollback();
        }
    }

    @Test
    @Order(40)
    void promoteCollection_unregisteredCollection_throwsAndWritesNoRow() {
        // RDR-204 Phase 1 (nexus-ft04v.7 gap 3, closed by nexus-ft04v.8's test
        // addendum): promoteCollection's own requireRegistered guard (step 4,
        // after the dim/null-embedding preconditions) must fail loud for a
        // collection with no catalog_collections row -- and create neither a
        // chunks row (nothing is staged for this collection) nor a
        // catalog_collections stub.
        String collection = "knowledge__unreg-promote__bge-base-en-v15-768__v1";

        assertThatThrownBy(() -> ops.promoteCollection(T1, collection, 768))
            .isInstanceOf(dev.nexus.service.db.UnregisteredCollectionException.class)
            .hasMessageContaining(collection)
            .hasMessageContaining("POST /v1/catalog/collections/upsert");

        assertThat(count(ctx -> ctx.selectCount().from(CHUNKS)
            .where(CHUNKS.COLLECTION.eq(collection))
            .fetchOne(0, Integer.class)))
            .as("a promote against an unregistered collection must create no chunks row")
            .isEqualTo(0);
        boolean stubRowExists = scope.withTenant(T1, ctx -> ctx.fetchExists(
                ctx.selectOne().from(CATALOG_COLLECTIONS)
                   .where(CATALOG_COLLECTIONS.TENANT_ID.eq(T1))
                   .and(CATALOG_COLLECTIONS.NAME.eq(collection))));
        assertThat(stubRowExists)
            .as("the rejected promote must not have created a catalog_collections stub row either")
            .isFalse();
    }
}
