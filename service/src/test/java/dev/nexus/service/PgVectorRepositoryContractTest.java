package dev.nexus.service;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.vectors.Embedder;
import dev.nexus.service.vectors.PgVectorRepository;
import liquibase.Contexts;
import liquibase.Liquibase;
import liquibase.database.Database;
import liquibase.database.DatabaseFactory;
import liquibase.database.jvm.JdbcConnection;
import liquibase.resource.ClassLoaderResourceAccessor;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.CsvSource;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.sql.PreparedStatement;
import java.sql.ResultSet;
import java.sql.SQLException;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.assertj.core.api.Assertions.within;

/**
 * RDR-155 P2.1 (bead nexus-duf53): VectorRepository pgvector contract suite.
 *
 * <p><strong>TDD-RED: every test in this class fails with
 * {@link UnsupportedOperationException} until bead nexus-tqeg6 (P2.2) implements
 * {@link PgVectorRepository}.</strong> The class itself ships as a signature-only skeleton
 * in this bead so the suite compiles; P2.2 fills the bodies and makes this suite green
 * WITHOUT changing it (the suite is the locked contract — exact counts, exact orderings).
 *
 * <p>Contract pinned here (RDR-155 §Proposed Solution, §Query path; §Approach item 2):
 * <ul>
 *   <li><strong>Runtime per-dim dispatch</strong> — the collection-name embedding-model
 *       segment routes to {@code nexus.chunks_384} / {@code chunks_768} / {@code chunks_1024}
 *       (RDR-103 collection-name authority); unknown segments fail loud, nothing written.
 *   <li><strong>Collection is a column</strong> — multi-collection search is a filtered
 *       union ({@code collection IN (...)}), one result list ordered by distance.
 *   <li><strong>Server-side embed unchanged</strong> — chunk TEXT in, vector stored; the
 *       stored vector is exactly the embedder's output.
 *   <li><strong>Tenant RLS scope through the repository API</strong> — every operation
 *       takes a tenant and another tenant sees/affects exactly 0 of its rows. All
 *       repository calls run as a plain LOGIN NOSUPERUSER NOBYPASSRLS role
 *       (nexus-5j7pb class — superuser would make every RLS assertion vacuous).
 *   <li><strong>RDR-108 manifest join</strong> — {@code documents.tumbler →
 *       catalog_document_chunks(collection, chash) → chunks_<dim>} resolves in-database,
 *       position-ordered; unresolvable manifest rows fail loud (application-enforced
 *       referential check, T2 nexus_rdr/155-manifest-fk-decision).
 * </ul>
 *
 * <p>Embedding in this suite is a deterministic {@link FakeEmbedder} (unit vectors with
 * known pairwise cosine distances) — integration is over the real Testcontainers pgvector
 * substrate, not over Voyage. The Voyage/CCE embedding-equivalence question is owned by
 * the inherited RDR-152 Phase 3 Seam B parity gate, not this suite.
 *
 * <p>Hermetic: Testcontainers pgvector/pgvector:pg17, PER_CLASS lifecycle.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class PgVectorRepositoryContractTest {

    // Plain-LOGIN service role: NOSUPERUSER, NOT table owner, NO BYPASSRLS.
    private static final String SVC_ROLE = "svc_vectors_test";
    private static final String SVC_PASS = "svc_vectors_test_pass";

    private static final String TENANT_A = "tenant-a";
    private static final String TENANT_B = "tenant-b";

    // One conformant collection name per dispatch target (model segment → dim).
    private static final String COL_CODE_1024 = "code__alpha__voyage-code-3__v1";
    private static final String COL_CTX_1024  = "knowledge__alpha__voyage-context-3__v1";
    private static final String COL_BGE_768   = "docs__alpha__bge-base-en-v15-768__v1";
    private static final String COL_MINI_384  = "knowledge__alpha__minilm-l6-v2-384__v1";
    private static final String COL_UNKNOWN   = "code__alpha__mystery-model-9000__v1";

    PostgreSQLContainer<?> pg;
    TenantScope tenantScope;
    HikariDataSource svcDs;

    FakeEmbedder embedder1024;
    FakeEmbedder embedder768;
    FakeEmbedder embedder384;

    PgVectorRepository repo1024;
    PgVectorRepository repo768;
    PgVectorRepository repo384;

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();

        // --- Step 1: create roles before Liquibase runs (changeset DO-blocks need them).
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            su.createStatement().execute(
                "DO $$ BEGIN " +
                "  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '" + SVC_ROLE + "') THEN " +
                // Explicit NOSUPERUSER NOBYPASSRLS (nexus-5j7pb): a privileged role here
                // would silently hollow out every cross-tenant assertion in this suite.
                "    CREATE ROLE " + SVC_ROLE + " LOGIN PASSWORD '" + SVC_PASS + "' NOSUPERUSER NOBYPASSRLS; " +
                "  END IF; " +
                "END $$");
            su.createStatement().execute(
                "DO $$ BEGIN " +
                "  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nexus_svc') THEN " +
                "    CREATE ROLE nexus_svc LOGIN PASSWORD 'nexus_svc_pass' NOSUPERUSER NOBYPASSRLS; " +
                "  END IF; " +
                "END $$");
        }

        // --- Step 2: apply the full master changelog via superuser (chunks tables exist
        //     since nexus-mf447; catalog tables since RDR-152).
        try (Connection su = pg.createConnection("")) {
            Database db = DatabaseFactory.getInstance()
                .findCorrectDatabaseImplementation(new JdbcConnection(su));
            Liquibase liquibase = new Liquibase(
                "db/changelog/db.changelog-master.xml",
                new ClassLoaderResourceAccessor(),
                db);
            liquibase.update(new Contexts());
        }

        // --- Step 3: grant schema access + DML on chunks tables + SELECT on the catalog
        //     tables the manifest join reads.
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            su.createStatement().execute("GRANT USAGE ON SCHEMA nexus TO " + SVC_ROLE);
            for (int dim : new int[] {384, 768, 1024}) {
                su.createStatement().execute(
                    "GRANT SELECT, INSERT, UPDATE, DELETE ON nexus.chunks_" + dim + " TO " + SVC_ROLE);
            }
            su.createStatement().execute(
                "GRANT SELECT ON nexus.catalog_documents, nexus.catalog_document_chunks TO " + SVC_ROLE);
            // RDR-156 P0.2: upsertChunks now auto-stubs catalog_collections before chunk writes.
            su.createStatement().execute(
                "GRANT SELECT, INSERT ON nexus.catalog_collections TO " + SVC_ROLE);
            su.createStatement().execute(
                "ALTER ROLE " + SVC_ROLE + " SET search_path TO nexus, public");
        }

        // --- Step 4: svc-role pool + TenantScope + repositories under test.
        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(SVC_ROLE);
        cfg.setPassword(SVC_PASS);
        cfg.setMaximumPoolSize(5);
        cfg.setAutoCommit(true);
        svcDs = new HikariDataSource(cfg);
        tenantScope = new TenantScope(svcDs);

        embedder1024 = new FakeEmbedder(1024);
        embedder768  = new FakeEmbedder(768);
        embedder384  = new FakeEmbedder(384);
        repo1024 = new PgVectorRepository(tenantScope, embedder1024, embedder1024);
        repo768  = new PgVectorRepository(tenantScope, embedder768,  embedder768);
        repo384  = new PgVectorRepository(tenantScope, embedder384,  embedder384);
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
        if (pg    != null) pg.stop();
    }

    // ---------------------------------------------------------------------------
    // Contract 1: model-segment → dim parse (RDR-103 collection-name authority)
    // ---------------------------------------------------------------------------

    @ParameterizedTest
    @CsvSource({
        "code__alpha__voyage-code-3__v1,           1024",
        "knowledge__alpha__voyage-context-3__v1,   1024",
        "docs__beta__voyage-context-3__v2,         1024",
        "knowledge__beta__voyage-3__v1,            1024",
        "docs__alpha__bge-base-en-v15-768__v1,     768",
        "knowledge__alpha__minilm-l6-v2-384__v1,   384",
    })
    void dimForCollection_knownModelTokens(String collection, int expectedDim) {
        assertThat(PgVectorRepository.dimForCollection(collection))
            .as("model segment of %s must dispatch to dim %d", collection, expectedDim)
            .isEqualTo(expectedDim);
    }

    @ParameterizedTest
    @CsvSource({
        "code__alpha__mystery-model-9000__v1",   // unknown model token
        "notacontenttype",                       // not four-segment conformant
        "code__alpha__v1",                       // missing model segment
    })
    void dimForCollection_unknownOrMalformed_failsLoud(String collection) {
        assertThatThrownBy(() -> PgVectorRepository.dimForCollection(collection))
            .as("unknown/malformed collection name must fail loud, never a fallback dim")
            .isInstanceOf(IllegalArgumentException.class);
    }

    // ---------------------------------------------------------------------------
    // Contract 2: upsert dispatches to exactly one chunks_<dim> table
    // ---------------------------------------------------------------------------

    @Test
    void upsert_voyageCode_landsInChunks1024Only() throws Exception {
        String col = COL_CODE_1024;
        repo1024.upsertChunks(TENANT_A, col,
            List.of("94e808c340ffefe12b14e350d67b5792bcff339466383cbb508a46d9e5996f7b", "03657eefff3cbc5f7fd114a1c03642869c722c8e44bdf863620a9d9f7fd35845"),
            List.of("dispatch text one", "dispatch text two"),
            List.of(Map.of("kind", "d"), Map.of("kind", "d")));

        assertThat(superuserCount(1024, col)).as("both rows in chunks_1024").isEqualTo(2L);
        assertThat(superuserCount(768,  col)).as("nothing in chunks_768").isEqualTo(0L);
        assertThat(superuserCount(384,  col)).as("nothing in chunks_384").isEqualTo(0L);
    }

    // ---------------------------------------------------------------------------
    // Contract: same-model vector PASSTHROUGH (nexus-hxry2) — supplied vectors are
    // stored verbatim, the embedder is NOT invoked, and a dim mismatch fails loud.
    // ---------------------------------------------------------------------------

    @Test
    void upsertChunksWithVectors_storesSuppliedVectorsVerbatim_skipsEmbedder() throws Exception {
        String col = COL_CTX_1024;
        String chash = "8701563d0025ccab2f02f5ef1e43ff4c4b4c6e543e61924ea3db9db1efa4095b";
        // A distinctive vector the FakeEmbedder would never produce (its default
        // is [1,0,...]); proves the stored vector is the SUPPLIED one, not embedded.
        float[] supplied = new float[1024];
        supplied[0] = 0.25f;
        supplied[1] = 0.75f;

        repo1024.upsertChunksWithVectors(TENANT_A, col,
            List.of(chash), List.of("passthrough text"),
            List.of(supplied), List.of(Map.of("kind", "pt")));

        assertThat(superuserCount(1024, col)).as("row landed in chunks_1024").isEqualTo(1L);
        String stored = superuserChunkEmbedding(1024, col, chash);
        assertThat(stored)
            .as("the SUPPLIED vector is stored verbatim, not the embedder's [1,0,...] default")
            .startsWith("[0.25,0.75,");
    }

    @Test
    void upsertChunksWithVectors_dimMismatch_failsLoud() {
        String col = COL_CTX_1024;  // dispatches to chunks_1024
        float[] wrongDim = new float[768];  // a 768-dim vector for a 1024 collection
        wrongDim[0] = 1.0f;
        // The contamination guard: a supplied vector whose dim disagrees with the
        // target table is rejected before any SQL — never silently stored or embedded.
        assertThatThrownBy(() -> repo1024.upsertChunksWithVectors(TENANT_A, col,
            List.of("a4c489a26d5080292a13e6121de83dfd1b38b504ace89284bfd6c05376ed3d80"), List.of("bad dim text"),
            List.of(wrongDim), List.of(Map.of())))
            .isInstanceOf(IllegalArgumentException.class);
    }

    @Test
    void upsertChunksWithVectors_lengthMismatch_failsLoud() {
        String col = COL_CTX_1024;
        float[] one = new float[1024];
        // Two ids, one embedding → fail loud (never misalign vectors to ids).
        assertThatThrownBy(() -> repo1024.upsertChunksWithVectors(TENANT_A, col,
            List.of("2992814a2981ef2baec5e8ab4795d73f89aec8f78aaf50333306499488100e8b", "d255289a4d63442514d3c1993d72997307a01d29d646c983ca4aebc25dded1c8"),
            List.of("t1", "t2"), List.of(one), List.of(Map.of(), Map.of())))
            .isInstanceOf(IllegalArgumentException.class);
    }

    @Test
    void upsert_voyage3_landsInChunks1024Only() throws Exception {
        String col = "knowledge__v3disp__voyage-3__v1";
        repo1024.upsertChunks(TENANT_A, col,
            List.of("8567d6f70cac9a4cccde28d9cb56a963f3d9b490ae47bc84cedeaeef2672c9a4"),
            List.of("voyage-3 dispatch text"),
            List.of(Map.of("kind", "d")));

        assertThat(superuserCount(1024, col)).as("row in chunks_1024").isEqualTo(1L);
        assertThat(superuserCount(768,  col)).as("nothing in chunks_768").isEqualTo(0L);
        assertThat(superuserCount(384,  col)).as("nothing in chunks_384").isEqualTo(0L);
    }

    /**
     * Postgres {@code text} cannot store NUL (0x00) bytes — Chroma and SQLite tolerated
     * them, so PDF-extraction noise carried NULs into chunk text for years (62 of 5,233
     * chunks in the production dt-papers collection, RDR-155 cloud-leg migration
     * 2026-06-10, bead nexus-rvfwj). Without sanitization the whole 300-chunk upsert
     * batch dies with {@code invalid byte sequence for encoding "UTF8": 0x00} — on the
     * serving path as well as the migration ETL. The repository strips NULs from chunk
     * text and metadata string values before bind; the chash is the caller's identity
     * and is never recomputed from the sanitized text.
     */
    @Test
    void upsert_nulBytesInTextAndMetadata_sanitizedNotRejected() throws Exception {
        String col = "knowledge__nulsan__voyage-context-3__v1";
        String dirty = "before\u0000middle\u0000\u0000after";
        repo1024.upsertChunks(TENANT_A, col,
            List.of("bd8c9cbb2dc5db921fb7ac8abe3ce14792c5ada3e71004f0a5d5fe9cb668e389", "ddbd25db5902224c81db2cc184cd523ccfa6e5ba91235925e4b5ee71a03107a8"),
            List.of(dirty, "clean text"),
            List.of(Map.of("note", "meta\u0000nul"), Map.of("kind", "clean")));

        assertThat(superuserCount(1024, col)).as("both rows landed despite NULs").isEqualTo(2L);
        assertThat(superuserChunkText(1024, col, "bd8c9cbb2dc5db921fb7ac8abe3ce14792c5ada3e71004f0a5d5fe9cb668e389"))
            .as("NULs stripped, surrounding text preserved verbatim")
            .isEqualTo("beforemiddleafter");
        assertThat(superuserChunkText(1024, col, "ddbd25db5902224c81db2cc184cd523ccfa6e5ba91235925e4b5ee71a03107a8"))
            .as("clean text untouched")
            .isEqualTo("clean text");
        assertThat(superuserChunkMetadataJson(1024, col, "bd8c9cbb2dc5db921fb7ac8abe3ce14792c5ada3e71004f0a5d5fe9cb668e389"))
            .as("metadata string values NUL-stripped too (jsonb rejects NUL like text)")
            .contains("\"note\"")
            .contains("\"metanul\"")
            .doesNotContain("u0000");
    }

    @Test
    void upsert_bge768_landsInChunks768Only() throws Exception {
        String col = COL_BGE_768;
        repo768.upsertChunks(TENANT_A, col,
            List.of("d95b4d08bd6a3d59c623808e4aa65cdac27f6a3bf7199a4d1d44cb3150ca8c37"),
            List.of("bge dispatch text"),
            List.of(Map.of("kind", "d")));

        assertThat(superuserCount(768,  col)).as("row in chunks_768").isEqualTo(1L);
        assertThat(superuserCount(1024, col)).as("nothing in chunks_1024").isEqualTo(0L);
        assertThat(superuserCount(384,  col)).as("nothing in chunks_384").isEqualTo(0L);
    }

    @Test
    void upsert_minilm384_landsInChunks384Only() throws Exception {
        String col = COL_MINI_384;
        repo384.upsertChunks(TENANT_A, col,
            List.of("51fec6a5786a3c03c6e250683460105f19b0cadaab7c5c242952dc83fd533aa6"),
            List.of("minilm dispatch text"),
            List.of(Map.of("kind", "d")));

        assertThat(superuserCount(384,  col)).as("row in chunks_384").isEqualTo(1L);
        assertThat(superuserCount(1024, col)).as("nothing in chunks_1024").isEqualTo(0L);
        assertThat(superuserCount(768,  col)).as("nothing in chunks_768").isEqualTo(0L);
    }

    @Test
    void upsert_unknownModelSegment_failsLoud_writesNothing() throws Exception {
        assertThatThrownBy(() ->
            repo1024.upsertChunks(TENANT_A, COL_UNKNOWN,
                List.of("f54714e93fc3fe982da5a57dda5096393e0402d57bfc8eeb8340fe9fdc1499b0"), List.of("unknown model text"), List.of(Map.of())))
            .as("unknown model segment must fail loud at dispatch")
            .isInstanceOf(IllegalArgumentException.class);

        for (int dim : new int[] {384, 768, 1024}) {
            assertThat(superuserCount(dim, COL_UNKNOWN))
                .as("no row may land in chunks_%d for the unknown-model collection", dim)
                .isEqualTo(0L);
        }
    }

    @Test
    void upsert_dimMismatchBetweenEmbedderAndTable_failsLoud_writesNothing() throws Exception {
        // 1024-dim embedder against a collection that dispatches to chunks_384:
        // must fail loud (no truncation, no padding), nothing written.
        String col = "knowledge__mismatch__minilm-l6-v2-384__v1";
        assertThatThrownBy(() ->
            repo1024.upsertChunks(TENANT_A, col,
                List.of("553160372ea5468b06889e7f82f46022562255cee824d0e7b112767ee0e28372"), List.of("mismatched vector"), List.of(Map.of())))
            .as("dim mismatch between embedded vector and dispatched table must throw")
            .isInstanceOf(Exception.class)
            // Not the skeleton's UOE: keeps this test RED until P2.2 actually implements
            // the write path (a bare isInstanceOf(Exception) passed vacuously against the
            // signature-only skeleton).
            .isNotInstanceOf(UnsupportedOperationException.class);

        assertThat(superuserCount(384, col))
            .as("no row may land in chunks_384 after the failed mismatch upsert")
            .isEqualTo(0L);
    }

    // ---------------------------------------------------------------------------
    // Contract 3: upsert semantics — stored vector, first-wins dedup, in-place
    // update, empty no-op, tenant attribution
    // ---------------------------------------------------------------------------

    @Test
    void upsert_storesExactEmbedderVector() throws Exception {
        String col = "code__exactvec__voyage-code-3__v1";
        embedder1024.register("exact vector text", 0.6f, 0.8f);
        repo1024.upsertChunks(TENANT_A, col,
            List.of("62d80cd8e01eb07e2ac46a24f43d995dcd168119e050e5896377646605e02250"), List.of("exact vector text"), List.of(Map.of()));

        double distToExpected = superuserCosineDistance(
            1024, col, "62d80cd8e01eb07e2ac46a24f43d995dcd168119e050e5896377646605e02250", FakeEmbedder.unitVector(1024, 0.6f, 0.8f));
        assertThat(distToExpected)
            .as("stored embedding must be exactly the embedder's output (cosine distance 0)")
            .isCloseTo(0.0, within(1e-6));
    }

    @Test
    void upsert_duplicateIdsInBatch_firstWins() throws Exception {
        String col = "code__dedup__voyage-code-3__v1";
        repo1024.upsertChunks(TENANT_A, col,
            List.of("c7f34fa21b84f8330b294ecdc364834f4ea71bd217a8d9b40626bd6ae36a4274", "c7f34fa21b84f8330b294ecdc364834f4ea71bd217a8d9b40626bd6ae36a4274"),
            List.of("first occurrence", "second occurrence"),
            List.of(Map.of("ord", "141f6a0a54dc647918a9ca503019cfb9638ba67bfc138e9b700373c8cb0babb6"), Map.of("ord", "f0c27c9db32a530fa251e7f7993dad00c9997744983a91caf96eed1e5745373e")));

        assertThat(superuserCount(1024, col))
            .as("duplicate IDs in one batch must collapse to exactly 1 row")
            .isEqualTo(1L);
        assertThat(superuserChunkText(1024, col, "c7f34fa21b84f8330b294ecdc364834f4ea71bd217a8d9b40626bd6ae36a4274"))
            .as("first-wins: the surviving row carries the FIRST text (T3Database._write_batch semantics)")
            .isEqualTo("first occurrence");
    }

    @Test
    void upsert_reUpsertSameChash_updatesInPlace() throws Exception {
        String col = "code__reupsert__voyage-code-3__v1";
        embedder1024.register("version one", 1.0f, 0.0f);
        embedder1024.register("version two", 0.0f, 1.0f);   // orthogonal to version one
        repo1024.upsertChunks(TENANT_A, col,
            List.of("c9659bbfeb7d33727401a1d85541e04af59f00574f3588e47ec0b55a6c74b60f"), List.of("version one"), List.of(Map.of("rev", "1")));
        repo1024.upsertChunks(TENANT_A, col,
            List.of("c9659bbfeb7d33727401a1d85541e04af59f00574f3588e47ec0b55a6c74b60f"), List.of("version two"), List.of(Map.of("rev", "2")));

        assertThat(superuserCount(1024, col))
            .as("re-upsert of the same chash must not create a second row")
            .isEqualTo(1L);
        assertThat(superuserChunkText(1024, col, "c9659bbfeb7d33727401a1d85541e04af59f00574f3588e47ec0b55a6c74b60f"))
            .as("re-upsert must update chunk_text in place (Chroma upsert semantics)")
            .isEqualTo("version two");
        assertThat(superuserCosineDistance(1024, col, "c9659bbfeb7d33727401a1d85541e04af59f00574f3588e47ec0b55a6c74b60f",
                FakeEmbedder.unitVector(1024, 0.0f, 1.0f)))
            .as("re-upsert must re-embed: the stored vector matches the NEW text's "
                + "embedding, not the original")
            .isCloseTo(0.0, within(1e-6));

        Map<String, Object> got = repo1024.get(TENANT_A, col, List.of("c9659bbfeb7d33727401a1d85541e04af59f00574f3588e47ec0b55a6c74b60f"), 10, 0);
        @SuppressWarnings("unchecked")
        List<Map<String, Object>> metas = (List<Map<String, Object>>) got.get("metadatas");
        assertThat(metas).hasSize(1);
        assertThat(metas.get(0).get("rev"))
            .as("re-upsert must replace metadata")
            .isEqualTo("2");
    }

    @Test
    void upsert_emptyIds_isNoop() throws Exception {
        String col = "code__emptybatch__voyage-code-3__v1";
        repo1024.upsertChunks(TENANT_A, col, List.of(), List.of(), List.of());
        assertThat(superuserCount(1024, col)).isEqualTo(0L);
    }

    @Test
    void upsert_rowsAttributedToCallingTenantOnly() throws Exception {
        String col = "code__attribution__voyage-code-3__v1";
        repo1024.upsertChunks(TENANT_A, col,
            List.of("49cada351f78d76282dd51bf0bee1fdcb7fb985d6a1f4b2338d4e7fda4abb0a1"), List.of("tenant a text"), List.of(Map.of()));

        assertThat(repo1024.count(TENANT_A, col))
            .as("writing tenant must see exactly its 1 row")
            .isEqualTo(1);
        assertThat(repo1024.count(TENANT_B, col))
            .as("other tenant must see exactly 0 rows through the repository API")
            .isEqualTo(0);
    }

    // ---------------------------------------------------------------------------
    // Contract 4: search — exact distance ordering, where predicate,
    // multi-collection union, nResults cap, tenant scope, row shape
    // ---------------------------------------------------------------------------

    /** Seed three chunks with known cosine distances to the query vector (1,0,...). */
    private void seedSearchFixture(String col) {
        embedder1024.register("search query", 1.0f, 0.0f);
        embedder1024.register("nearest text", 1.0f, 0.0f);     // distance 0.0
        embedder1024.register("middle text",  0.8f, 0.6f);     // distance 0.2
        embedder1024.register("farthest text", 0.0f, 1.0f);    // distance 1.0
        repo1024.upsertChunks(TENANT_A, col,
            List.of("3642f56d48b72b6bf43456f6c1a451d6625e9efc5bed097f381214eca5998b3e", "7335e096cdd5c417e4c2f31a9b58bdb93f2fa8a4c81acd9efd39c405f7e18f28", "8ecea065b8b1777ed87fa27510795344fcb20d7312c5be5965882374f971d014"),
            List.of("nearest text", "middle text", "farthest text"),
            List.of(Map.of("kind", "a"), Map.of("kind", "b"), Map.of("kind", "a")));
    }

    @Test
    void search_ordersByCosineDistance_exactOrderAndValues() {
        String col = "code__searchorder__voyage-code-3__v1";
        seedSearchFixture(col);

        List<Map<String, Object>> rows =
            repo1024.search(TENANT_A, "search query", List.of(col), 10, null);

        assertThat(rows).as("exactly the 3 seeded rows").hasSize(3);
        assertThat(rows).extracting(r -> r.get("id"))
            .as("distance-ascending order is exact")
            .containsExactly("3642f56d48b72b6bf43456f6c1a451d6625e9efc5bed097f381214eca5998b3e", "7335e096cdd5c417e4c2f31a9b58bdb93f2fa8a4c81acd9efd39c405f7e18f28", "8ecea065b8b1777ed87fa27510795344fcb20d7312c5be5965882374f971d014");
        assertThat(((Number) rows.get(0).get("distance")).doubleValue()).isCloseTo(0.0, within(1e-5));
        assertThat(((Number) rows.get(1).get("distance")).doubleValue()).isCloseTo(0.2, within(1e-5));
        assertThat(((Number) rows.get(2).get("distance")).doubleValue()).isCloseTo(1.0, within(1e-5));

        // Row shape: id, content, distance, collection + metadata flattened in.
        Map<String, Object> first = rows.get(0);
        assertThat(first.get("content")).isEqualTo("nearest text");
        assertThat(first.get("collection")).isEqualTo(col);
        assertThat(first.get("kind"))
            .as("chunk metadata keys must be flattened into the row")
            .isEqualTo("a");
    }

    @Test
    void search_wherePredicate_filtersExactly() {
        String col = "code__searchwhere__voyage-code-3__v1";
        seedSearchFixture(col);

        List<Map<String, Object>> rows = repo1024.search(
            TENANT_A, "search query", List.of(col), 10, Map.of("kind", "a"));

        assertThat(rows).extracting(r -> r.get("id"))
            .as("where {kind=a} must return exactly the two matching rows, distance-ordered")
            .containsExactly("3642f56d48b72b6bf43456f6c1a451d6625e9efc5bed097f381214eca5998b3e", "8ecea065b8b1777ed87fa27510795344fcb20d7312c5be5965882374f971d014");
    }

    @Test
    void search_wherePredicate_multiKeyIsAnded() {
        String col = "code__searchand__voyage-code-3__v1";
        embedder1024.register("search query", 1.0f, 0.0f);
        embedder1024.register("and t1", 1.0f, 0.0f);
        embedder1024.register("and t2", 0.8f, 0.6f);
        embedder1024.register("and t3", 0.0f, 1.0f);
        repo1024.upsertChunks(TENANT_A, col,
            List.of("cdf84c7df20ac7dad0f684d01d839a593947fd0e199468a7e9a88e840da8f6dc", "e41c8e6673a1a30b900633a26c531936f2fbd72600b9b596bd2ea39bc44b15a2", "e3aa05251298daa7e76e84d1842f6f8ea82f309e8b172bee27ea99d5afe0ef10"),
            List.of("and t1", "and t2", "and t3"),
            List.of(Map.of("kind", "a", "score", "high"),
                    Map.of("kind", "a", "score", "low"),
                    Map.of("kind", "b", "score", "high")));

        List<Map<String, Object>> rows = repo1024.search(
            TENANT_A, "search query", List.of(col), 10,
            Map.of("kind", "a", "score", "high"));

        assertThat(rows).extracting(r -> r.get("id"))
            .as("multi-key where must AND all predicates: exactly the one row matching both")
            .containsExactly("cdf84c7df20ac7dad0f684d01d839a593947fd0e199468a7e9a88e840da8f6dc");
    }

    // Contract 4b: operator-form where-filters on the bridge (nexus-05bfd).
    // seedSearchFixture: snear{kind=a} d0.0, smid{kind=b} d0.2, sfar{kind=a} d1.0.

    @Test
    void search_whereNe_excludesMatching_distanceOrdered() {
        String col = "code__searchne__voyage-code-3__v1";
        seedSearchFixture(col);

        List<Map<String, Object>> rows = repo1024.search(
            TENANT_A, "search query", List.of(col), 10,
            Map.of("kind", Map.of("$ne", "b")));

        assertThat(rows).extracting(r -> r.get("id"))
            .as("{kind:{$ne:b}} drops the one kind=b row, keeps both kind=a, distance-ordered")
            .containsExactly("3642f56d48b72b6bf43456f6c1a451d6625e9efc5bed097f381214eca5998b3e", "8ecea065b8b1777ed87fa27510795344fcb20d7312c5be5965882374f971d014");
    }

    // Contract 4c: range operators, operand-typed (nexus-4l80g).

    @Test
    void search_whereGteNumeric_matchesJsonNumbersOnly_neverCastErrors() {
        String col = "code__searchgte__voyage-code-3__v1";
        embedder1024.register("search query", 1.0f, 0.0f);
        embedder1024.register("year t1", 1.0f, 0.0f);
        embedder1024.register("year t2", 0.8f, 0.6f);
        embedder1024.register("year t3", 0.6f, 0.8f);
        embedder1024.register("year t4", 0.0f, 1.0f);
        repo1024.upsertChunks(TENANT_A, col,
            List.of("5fe59261ef2deb5a71a465d26a552dab0b6ed500a23ec72393d9fc7a7beb21b1", "aee331e61a5aef71a62ac8ae0dc96e5cbec121c9ca8399fabca6ae601aff2feb",
                    "fc926f894506caf7e314ee2d88ae6d5c9aeca62aa307e0d1608757f8cf70e06b", "16994366be46210ec48da3aba917a2b985b51c8c64561ed4d74a7ea0981b4c4e"),
            List.of("year t1", "year t2", "year t3", "year t4"),
            List.of(Map.of("bib_year", 2021),
                    Map.of("bib_year", 2020),
                    Map.of("bib_year", 2019),
                    // JSON-STRING year: must be excluded by a numeric operand
                    // WITHOUT aborting the query on the ::numeric cast.
                    Map.of("bib_year", "not-a-year")));

        List<Map<String, Object>> rows = repo1024.search(
            TENANT_A, "search query", List.of(col), 10,
            Map.of("bib_year", Map.of("$gte", 2020)));

        assertThat(rows).extracting(r -> r.get("id"))
            .as("{bib_year:{$gte:2020}} matches the two JSON-number rows >= 2020, "
                + "excludes 2019 AND the non-numeric row, distance-ordered")
            .containsExactly("5fe59261ef2deb5a71a465d26a552dab0b6ed500a23ec72393d9fc7a7beb21b1", "aee331e61a5aef71a62ac8ae0dc96e5cbec121c9ca8399fabca6ae601aff2feb");
    }

    @Test
    void search_whereGtString_isLexical_theDocumentedNineVsTenHazard() {
        String col = "code__searchlex__voyage-code-3__v1";
        embedder1024.register("search query", 1.0f, 0.0f);
        embedder1024.register("lex t1", 1.0f, 0.0f);
        embedder1024.register("lex t2", 0.0f, 1.0f);
        repo1024.upsertChunks(TENANT_A, col,
            List.of("b591079be72686a720fbe0651e73527918ba6e373fdaebfd379342083af038b4", "10790a4789f6239227283bdb50218cc6ee32776e07538994d29a6082be827469"),
            List.of("lex t1", "lex t2"),
            List.of(Map.of("rank", "9"), Map.of("rank", "10")));

        List<Map<String, Object>> rows = repo1024.search(
            TENANT_A, "search query", List.of(col), 10,
            Map.of("rank", Map.of("$gt", "10")));

        assertThat(rows).extracting(r -> r.get("id"))
            .as("string operand compares LEXICALLY by design: '9' > '10' matches, "
                + "'10' > '10' does not — numeric intent must pass a number")
            .containsExactly("b591079be72686a720fbe0651e73527918ba6e373fdaebfd379342083af038b4");
    }

    @Test
    @SuppressWarnings("unchecked")
    void getWhere_gteNumeric_jooqTwinMatchesAppendWherePredicate() {
        // Review c0e4493e finding 2: metadataCondition (the jOOQ twin used by
        // getWhere/getAllMetadata) carries its own range-operator branch —
        // "the two translators must not drift" needs DB-level proof on BOTH,
        // not just the search() path.
        String col = "code__getwheregte__voyage-code-3__v1";
        embedder1024.register("gw t1", 1.0f, 0.0f);
        embedder1024.register("gw t2", 0.8f, 0.6f);
        embedder1024.register("gw t3", 0.0f, 1.0f);
        repo1024.upsertChunks(TENANT_A, col,
            List.of("023b5a0797ab810e8a41d1fa6cf304ceb2e3efbcb18813340c3448f62870f65e", "49e273168c155793fabc9392608f4b6d9c40b050020b6ad8fcaf1e019e624eeb",
                    "dd8e3600d58b8dbb83bd696895333dcf153c0ff6ce27479f56925a1a2ba0c7b1"),
            List.of("gw t1", "gw t2", "gw t3"),
            List.of(Map.of("bib_year", 2021),
                    Map.of("bib_year", 2019),
                    Map.of("bib_year", "not-a-year")));

        Map<String, Object> env = repo1024.getWhere(
            TENANT_A, col, Map.of("bib_year", Map.of("$gte", 2020)), 10, 0, false);

        assertThat((List<String>) env.get("ids"))
            .as("jOOQ twin: numeric $gte matches only JSON-number rows >= 2020, "
                + "excludes 2019 and the non-numeric row, no cast error")
            .containsExactly("023b5a0797ab810e8a41d1fa6cf304ceb2e3efbcb18813340c3448f62870f65e");
    }

    @Test
    void search_whereEqOperatorForm_matchesPlainEquality() {
        String col = "code__searcheq__voyage-code-3__v1";
        seedSearchFixture(col);

        List<Map<String, Object>> rows = repo1024.search(
            TENANT_A, "search query", List.of(col), 10,
            Map.of("kind", Map.of("$eq", "a")));

        assertThat(rows).extracting(r -> r.get("id"))
            .as("{kind:{$eq:a}} is identical to plain {kind:a}")
            .containsExactly("3642f56d48b72b6bf43456f6c1a451d6625e9efc5bed097f381214eca5998b3e", "8ecea065b8b1777ed87fa27510795344fcb20d7312c5be5965882374f971d014");
    }

    @Test
    void search_whereIn_matchesAnyListed() {
        String col = "code__searchin__voyage-code-3__v1";
        seedSearchFixture(col);

        assertThat(repo1024.search(TENANT_A, "search query", List.of(col), 10,
                Map.of("kind", Map.of("$in", List.of("b")))))
            .as("{kind:{$in:[b]}} returns exactly the kind=b row")
            .extracting(r -> r.get("id"))
            .containsExactly("7335e096cdd5c417e4c2f31a9b58bdb93f2fa8a4c81acd9efd39c405f7e18f28");

        assertThat(repo1024.search(TENANT_A, "search query", List.of(col), 10,
                Map.of("kind", Map.of("$in", List.of("a", "b")))))
            .as("{kind:{$in:[a,b]}} returns all three, distance-ordered")
            .extracting(r -> r.get("id"))
            .containsExactly("3642f56d48b72b6bf43456f6c1a451d6625e9efc5bed097f381214eca5998b3e", "7335e096cdd5c417e4c2f31a9b58bdb93f2fa8a4c81acd9efd39c405f7e18f28", "8ecea065b8b1777ed87fa27510795344fcb20d7312c5be5965882374f971d014");
    }

    @Test
    void search_whereNin_excludesListed() {
        String col = "code__searchnin__voyage-code-3__v1";
        seedSearchFixture(col);

        List<Map<String, Object>> rows = repo1024.search(
            TENANT_A, "search query", List.of(col), 10,
            Map.of("kind", Map.of("$nin", List.of("b"))));

        assertThat(rows).extracting(r -> r.get("id"))
            .as("{kind:{$nin:[b]}} excludes kind=b, keeps both kind=a")
            .containsExactly("3642f56d48b72b6bf43456f6c1a451d6625e9efc5bed097f381214eca5998b3e", "8ecea065b8b1777ed87fa27510795344fcb20d7312c5be5965882374f971d014");
    }

    @Test
    void search_whereNe_keepsRowsMissingTheKey() {
        // Chroma "field != value" intent: a row that carries no `section_type` at all
        // must be KEPT when filtering section_type != references (IS DISTINCT FROM, not !=).
        String col = "code__searchnemiss__voyage-code-3__v1";
        embedder1024.register("search query", 1.0f, 0.0f);
        embedder1024.register("has key", 1.0f, 0.0f);
        embedder1024.register("no key", 0.8f, 0.6f);
        repo1024.upsertChunks(TENANT_A, col,
            List.of("b51ac24c6bc45baa99e787042722c6b72826d006ac62e7b34e8edf2a50e1268d", "839dfe07d520ef67392aad6c4ed6fd1cf22ad8145df7430ef58f9f9154e3a036"),
            List.of("has key", "no key"),
            List.of(Map.of("section_type", "references"), Map.of("other", "x")));

        List<Map<String, Object>> rows = repo1024.search(
            TENANT_A, "search query", List.of(col), 10,
            Map.of("section_type", Map.of("$ne", "references")));

        assertThat(rows).extracting(r -> r.get("id"))
            .as("$ne keeps the row whose section_type is ABSENT, drops the explicit references row")
            .containsExactly("839dfe07d520ef67392aad6c4ed6fd1cf22ad8145df7430ef58f9f9154e3a036");
    }

    @Test
    void search_whereUnsupportedOperator_failsLoud() {
        String col = "code__searchbadop__voyage-code-3__v1";
        seedSearchFixture(col);

        assertThatThrownBy(() -> repo1024.search(TENANT_A, "search query", List.of(col), 10,
                Map.of("kind", Map.of("$regex", "a.*"))))
            .as("an unsupported operator must fail loud (400), not silently match nothing")
            .isInstanceOf(IllegalArgumentException.class)
            .hasMessageContaining("$regex");
    }

    @Test
    void search_whereCompoundOperator_failsLoud() {
        String col = "code__searchcompound__voyage-code-3__v1";
        seedSearchFixture(col);

        assertThatThrownBy(() -> repo1024.search(TENANT_A, "search query", List.of(col), 10,
                Map.of("$or", List.of(Map.of("kind", "a"), Map.of("kind", "b")))))
            .as("compound $or is not supported and must fail loud, not bind as a literal column")
            .isInstanceOf(IllegalArgumentException.class)
            .hasMessageContaining("$or");
    }

    @Test
    void getWhere_operatorForm_filtersExactly() {
        // The same translator backs getWhere (store-get with where) — exercise the
        // second call site so its bind ordering is covered too.
        String col = "code__getwherene__voyage-code-3__v1";
        embedder1024.register("gw a", 1.0f, 0.0f);
        embedder1024.register("gw b", 0.8f, 0.6f);
        repo1024.upsertChunks(TENANT_A, col,
            List.of("6064e40df1d4cc70c0a6a080696aa20b39ec6a2a59055c01759c40a00efdefb4", "9ba2c99c69294614a99cb03d29f1d5a69fcd960dc776384f06635c33b7511b9f"),
            List.of("gw a", "gw b"),
            List.of(Map.of("kind", "a"), Map.of("kind", "b")));

        @SuppressWarnings("unchecked")
        List<String> ids = (List<String>) repo1024.getWhere(
            TENANT_A, col, Map.of("kind", Map.of("$ne", "b")), 100, 0).get("ids");

        assertThat(ids)
            .as("getWhere {kind:{$ne:b}} returns only the kind=a chunk")
            .containsExactly("6064e40df1d4cc70c0a6a080696aa20b39ec6a2a59055c01759c40a00efdefb4");
    }

    @Test
    void getAllMetadata_returnsEveryChunkInOneCall_noDocuments() {
        // nexus-duoak follow-up: collapses the staleness-cache-build phase's
        // paginated /get loop into one round trip. No "documents" key in the
        // response -- staleness only needs metadata, keeping the payload lean.
        String col = "code__getallmeta__voyage-code-3__v1";
        embedder1024.register("gam a", 1.0f, 0.0f);
        embedder1024.register("gam b", 0.8f, 0.6f);
        embedder1024.register("gam c", 0.6f, 0.8f);
        repo1024.upsertChunks(TENANT_A, col,
            List.of("7086d8172bf21e7bafab4a9e75ae641ab02fad3766cc46dc3f92d4ec21f01ef2", "9275ef20511b0558b912abcc5233b7572125d0d8546302ebccf7d791d16ca722",
                    "1e280e2fcca5f0a0c390aebb98a906c9b9b97aaa486f02b74022dafbe809ca44"),
            List.of("gam a", "gam b", "gam c"),
            List.of(Map.of("chunk_text_hash", "ha"), Map.of("chunk_text_hash", "hb"),
                    Map.of("chunk_text_hash", "hc")));

        var result = repo1024.getAllMetadata(TENANT_A, col, null);

        assertThat(result).doesNotContainKey("documents");
        @SuppressWarnings("unchecked")
        List<String> ids = (List<String>) result.get("ids");
        assertThat(ids).containsExactlyInAnyOrder(
            "7086d8172bf21e7bafab4a9e75ae641ab02fad3766cc46dc3f92d4ec21f01ef2", "9275ef20511b0558b912abcc5233b7572125d0d8546302ebccf7d791d16ca722",
            "1e280e2fcca5f0a0c390aebb98a906c9b9b97aaa486f02b74022dafbe809ca44");
        @SuppressWarnings("unchecked")
        List<Map<String, Object>> metas = (List<Map<String, Object>>) result.get("metadatas");
        assertThat(metas).hasSize(3);
        assertThat(metas).extracting(m -> m.get("chunk_text_hash"))
            .containsExactlyInAnyOrder("ha", "hb", "hc");
    }

    @Test
    void getAllMetadata_withWhere_filtersExactly() {
        String col = "code__getallmetawhere__voyage-code-3__v1";
        embedder1024.register("gamw a", 1.0f, 0.0f);
        embedder1024.register("gamw b", 0.8f, 0.6f);
        repo1024.upsertChunks(TENANT_A, col,
            List.of("7a7811b3e36783ce46ffb0719820d6df5fb9c6f082aed0265eaca5c5c2ab7724", "e6e16fbe4391c1f117e404b2f64c3fe18c4de74f381d330d0603286470ceaca5"),
            List.of("gamw a", "gamw b"),
            List.of(Map.of("kind", "a"), Map.of("kind", "b")));

        @SuppressWarnings("unchecked")
        List<String> ids = (List<String>) repo1024.getAllMetadata(
            TENANT_A, col, Map.of("kind", "a")).get("ids");

        assertThat(ids).containsExactly("7a7811b3e36783ce46ffb0719820d6df5fb9c6f082aed0265eaca5c5c2ab7724");
    }

    @Test
    void getAllMetadata_scopedToTenant_noCrossTenantLeak() {
        String col = "code__getallmetatenant__voyage-code-3__v1";
        embedder1024.register("gamt a", 1.0f, 0.0f);
        embedder1024.register("gamt b", 0.8f, 0.6f);
        repo1024.upsertChunks(TENANT_A, col,
            List.of("e13b98527d51d094e2844dabad9621679e6da3e7af4b20c5bfe11063f8b031e8"), List.of("gamt a"), List.of(Map.of()));
        repo1024.upsertChunks(TENANT_B, col,
            List.of("37dc922045704dde983f53067a0b6b4e108cbb533b66cd4df45a677edd701a61"), List.of("gamt b"), List.of(Map.of()));

        @SuppressWarnings("unchecked")
        List<String> idsA = (List<String>) repo1024.getAllMetadata(TENANT_A, col, null).get("ids");

        assertThat(idsA).containsExactly("e13b98527d51d094e2844dabad9621679e6da3e7af4b20c5bfe11063f8b031e8");
    }

    @Test
    void getAllMetadata_emptyCollection_returnsEmptyLists() {
        var result = repo1024.getAllMetadata(
            TENANT_A, "code__getallmetaempty__voyage-code-3__v1", null);

        assertThat((List<?>) result.get("ids")).isEmpty();
        assertThat((List<?>) result.get("metadatas")).isEmpty();
    }

    @Test
    void search_mixedDimCollections_failsLoud() {
        // COL_CODE_1024 dispatches to chunks_1024, COL_BGE_768 to chunks_768: one query
        // vector cannot serve both spaces. Must fail loud, never silently skip or union.
        assertThatThrownBy(() ->
            repo1024.search(TENANT_A, "search query",
                List.of(COL_CODE_1024, COL_BGE_768), 10, null))
            .as("mixing dims across collections in one search call must fail loud")
            .isInstanceOf(IllegalArgumentException.class);
    }

    @Test
    void search_multiCollection_unionOrderedByDistance() {
        String colX = "code__searchmcx__voyage-code-3__v1";
        String colY = "code__searchmcy__voyage-code-3__v1";
        embedder1024.register("search query", 1.0f, 0.0f);
        embedder1024.register("x near", 1.0f, 0.0f);       // 0.0
        embedder1024.register("y mid",  0.8f, 0.6f);       // 0.2
        embedder1024.register("x far",  0.0f, 1.0f);       // 1.0
        repo1024.upsertChunks(TENANT_A, colX,
            List.of("ae82004549f8d15bc982e6241e8e457e86fc64419f4085aacde5955d5e6ce310", "dbefc10c312a22493703ac60b85c1328be6705505787da3c48af6a6ca0a7b369"), List.of("x near", "x far"),
            List.of(Map.of(), Map.of()));
        repo1024.upsertChunks(TENANT_A, colY,
            List.of("4be89caf1def5bb4c8a00538a6d0588a35bc64a0940e79d82b44e1c03be6d434"), List.of("y mid"), List.of(Map.of()));

        List<Map<String, Object>> rows = repo1024.search(
            TENANT_A, "search query", List.of(colX, colY), 10, null);

        assertThat(rows).extracting(r -> r.get("id"))
            .as("multi-collection union must interleave by distance, not group by collection")
            .containsExactly("ae82004549f8d15bc982e6241e8e457e86fc64419f4085aacde5955d5e6ce310", "4be89caf1def5bb4c8a00538a6d0588a35bc64a0940e79d82b44e1c03be6d434", "dbefc10c312a22493703ac60b85c1328be6705505787da3c48af6a6ca0a7b369");
        assertThat(rows).extracting(r -> r.get("collection"))
            .as("each row labels its source collection")
            .containsExactly(colX, colY, colX);
    }

    @Test
    void search_nResults_capsRowCount() {
        String col = "code__searchcap__voyage-code-3__v1";
        seedSearchFixture(col);

        List<Map<String, Object>> rows =
            repo1024.search(TENANT_A, "search query", List.of(col), 2, null);

        assertThat(rows).extracting(r -> r.get("id"))
            .as("nResults=2 returns exactly the 2 nearest")
            .containsExactly("3642f56d48b72b6bf43456f6c1a451d6625e9efc5bed097f381214eca5998b3e", "7335e096cdd5c417e4c2f31a9b58bdb93f2fa8a4c81acd9efd39c405f7e18f28");
    }

    @Test
    void search_emptyCollectionList_returnsEmpty() {
        assertThat(repo1024.search(TENANT_A, "any query", List.of(), 10, null))
            .as("an empty collection list yields an empty result, not an error")
            .isEmpty();
    }

    @Test
    void search_crossTenant_returnsNothing() throws Exception {
        String col = "code__searchrls__voyage-code-3__v1";
        seedSearchFixture(col);
        assertThat(superuserCount(1024, col))
            .as("control: the 3 seeded rows must physically exist before the RLS assertion")
            .isEqualTo(3L);

        List<Map<String, Object>> rows =
            repo1024.search(TENANT_B, "search query", List.of(col), 10, null);

        assertThat(rows)
            .as("tenant-b must get exactly 0 results from tenant-a's rows")
            .isEmpty();
    }

    // ---------------------------------------------------------------------------
    // Contract 5: get / list / count
    // ---------------------------------------------------------------------------

    @Test
    void get_byIds_returnsAlignedEnvelope_missingIdsOmitted() {
        String col = "code__getbyids__voyage-code-3__v1";
        repo1024.upsertChunks(TENANT_A, col,
            List.of("7f64f08894a0f056e76277f5754f0fcbacc466e4b54d69e73d524b18f76b1534", "e72bc85c6d674d98ac4a49bc4bdbb624e18b20e971cf63028e0b1d991b83ed8e"),
            List.of("get text one", "get text two"),
            List.of(Map.of("m", "1"), Map.of("m", "2")));

        Map<String, Object> got = repo1024.get(
            TENANT_A, col, List.of("7f64f08894a0f056e76277f5754f0fcbacc466e4b54d69e73d524b18f76b1534", "e72bc85c6d674d98ac4a49bc4bdbb624e18b20e971cf63028e0b1d991b83ed8e", "f2318f74905dd169f2832e369cde730cf93957ef73a9965e14a982f85fcd8be7"), 10, 0);

        @SuppressWarnings("unchecked")
        List<String> ids = (List<String>) got.get("ids");
        @SuppressWarnings("unchecked")
        List<String> docs = (List<String>) got.get("documents");
        @SuppressWarnings("unchecked")
        List<Map<String, Object>> metas = (List<Map<String, Object>>) got.get("metadatas");

        assertThat(ids).as("missing id omitted, both present ids returned")
            .containsExactlyInAnyOrder("7f64f08894a0f056e76277f5754f0fcbacc466e4b54d69e73d524b18f76b1534", "e72bc85c6d674d98ac4a49bc4bdbb624e18b20e971cf63028e0b1d991b83ed8e");
        assertThat(docs).hasSize(2);
        assertThat(metas).hasSize(2);
        int i1 = ids.indexOf("7f64f08894a0f056e76277f5754f0fcbacc466e4b54d69e73d524b18f76b1534");
        assertThat(docs.get(i1)).isEqualTo("get text one");
        assertThat(metas.get(i1).get("m")).isEqualTo("1");
    }

    @Test
    void get_limitOffset_skipsInChashOrder() {
        String col = "code__getoffset__voyage-code-3__v1";
        // chashFirst < chashSecond lexically. Insertion order is the REVERSE of
        // chash order, so this assertion can distinguish chash-order pagination
        // from insertion-order or id-list-order pagination: those would return
        // chashFirst (the FIRST-inserted item) at offset 1, not chashSecond.
        String chashFirst  = "87b185c15a40d28f7d7484e49fb2081802ccf0deb6dbbbe6bbca4680fe54ab5c";
        String chashSecond = "c2d86e18b4a378e1f67def0f9d2c30fd963ea2b4f9c17657c6fc1d9c4cb97984";
        repo1024.upsertChunks(TENANT_A, col,
            List.of(chashSecond, chashFirst),
            List.of("offset text z", "offset text a"),
            List.of(Map.of(), Map.of()));

        Map<String, Object> got = repo1024.get(
            TENANT_A, col, List.of(chashSecond, chashFirst), 1, 1);
        @SuppressWarnings("unchecked")
        List<String> ids = (List<String>) got.get("ids");

        assertThat(ids)
            .as("limit=1 offset=1 must skip the first chash and return the second")
            .containsExactly(chashSecond);
    }

    @Test
    void get_crossTenant_returnsEmptyEnvelope() throws Exception {
        String col = "code__getrls__voyage-code-3__v1";
        repo1024.upsertChunks(TENANT_A, col,
            List.of("8590a6065d553413999657b9a41bf59b8ec98e3506220b0d87535cf85a94590a"), List.of("tenant a only"), List.of(Map.of()));
        assertThat(superuserCount(1024, col))
            .as("control: tenant-a's row must physically exist before the RLS assertion")
            .isEqualTo(1L);

        Map<String, Object> got = repo1024.get(TENANT_B, col, List.of("8590a6065d553413999657b9a41bf59b8ec98e3506220b0d87535cf85a94590a"), 10, 0);
        @SuppressWarnings("unchecked")
        List<String> ids = (List<String>) got.get("ids");
        assertThat(ids).as("tenant-b must get 0 of tenant-a's chunks").isEmpty();
    }

    @Test
    void list_paginatesWithLimitOffset_disjointAndComplete() {
        String col = "code__listpage__voyage-code-3__v1";
        List<String> allIds = List.of("edeb3550179717809d89305e664435d7d49d1c762314df4766ced1a5ccb7ddf8", "a290318d3c8fc6798d9130317ac6d333dd49d0c06f4334c7a03be55c5381d208", "916b0e73565360835673e97d17749830c6bbc8b733526a9043d06cb491cb0fe4", "f9c44439c1f001cd1720cbeb4dd6dab03aaccb6e24b85ec3fc0849027cdf8d16", "8d77625dd7c52ccc36824061ccddd56d1a23d4e07da41a04bdbc105f5c88bdae");
        repo1024.upsertChunks(TENANT_A, col, allIds,
            List.of("t1", "t2", "t3", "t4", "t5"),
            List.of(Map.of(), Map.of(), Map.of(), Map.of(), Map.of()));

        @SuppressWarnings("unchecked")
        List<String> page1 = (List<String>) repo1024.list(TENANT_A, col, 3, 0).get("ids");
        @SuppressWarnings("unchecked")
        List<String> page2 = (List<String>) repo1024.list(TENANT_A, col, 3, 3).get("ids");

        // Pagination is by chash ordering (skeleton javadoc) and the seeded ids are
        // lexicographically ordered, so each page's exact contents are pinned — an
        // unstable sort would make repeated list() calls return different rows.
        assertThat(page1).as("first page: exactly the 3 lowest chashes, in order")
            .containsExactly("8d77625dd7c52ccc36824061ccddd56d1a23d4e07da41a04bdbc105f5c88bdae", "916b0e73565360835673e97d17749830c6bbc8b733526a9043d06cb491cb0fe4", "a290318d3c8fc6798d9130317ac6d333dd49d0c06f4334c7a03be55c5381d208");
        assertThat(page2).as("second page: exactly the remaining 2 chashes, in order")
            .containsExactly("edeb3550179717809d89305e664435d7d49d1c762314df4766ced1a5ccb7ddf8", "f9c44439c1f001cd1720cbeb4dd6dab03aaccb6e24b85ec3fc0849027cdf8d16");
        Set<String> union = new LinkedHashSet<>();
        union.addAll(page1);
        union.addAll(page2);
        assertThat(union)
            .as("pages are disjoint and together cover all 5 ids")
            .containsExactlyInAnyOrderElementsOf(allIds);
    }

    @Test
    void list_crossTenant_returnsEmptyEnvelope() throws Exception {
        String col = "code__listrls__voyage-code-3__v1";
        repo1024.upsertChunks(TENANT_A, col,
            List.of("d9d97e7374edd100822857552035df2e9dce9a41c9074a10d3f7b77344d345f6"), List.of("tenant a list row"), List.of(Map.of()));
        assertThat(superuserCount(1024, col))
            .as("control: tenant-a's row must physically exist before the RLS assertion")
            .isEqualTo(1L);

        @SuppressWarnings("unchecked")
        List<String> ids = (List<String>) repo1024.list(TENANT_B, col, 10, 0).get("ids");
        assertThat(ids).as("tenant-b must list 0 of tenant-a's chunks").isEmpty();
    }

    @Test
    void count_exactPerCollection_andZeroForOtherTenant() {
        String col = "code__countexact__voyage-code-3__v1";
        repo1024.upsertChunks(TENANT_A, col,
            List.of("e97ee5eff30b9a90fae02685d46ec1a8348f898a5909b9af34ec9f0e41e05089", "eea04dbaea10efbb5ee9f52285848b35a49ad46e7795e240092057033160cf2e", "f740a25a37f48241e662037a0024d9522843ac867506630ce4dc0f45ff74c924"),
            List.of("a", "b", "c"),
            List.of(Map.of(), Map.of(), Map.of()));

        assertThat(repo1024.count(TENANT_A, col)).isEqualTo(3);
        assertThat(repo1024.count(TENANT_B, col)).isEqualTo(0);
        assertThat(repo1024.count(TENANT_A, "code__neverwritten__voyage-code-3__v1"))
            .as("a collection with no rows counts 0, not an error")
            .isEqualTo(0);
    }

    // ---------------------------------------------------------------------------
    // Contract 6: delete
    // ---------------------------------------------------------------------------

    @Test
    void delete_byIds_returnsAffectedCount() {
        String col = "code__deleteown__voyage-code-3__v1";
        repo1024.upsertChunks(TENANT_A, col,
            List.of("e671f06a02abb1132e8ca202221695264e2f8118d3128ab13e29fa04c610584b", "47e66b7e0eb19e987de506efaea585e504734c8422d39fab7d247551a9da5c0d", "6f23bf657e7511d4e5dd51ee9bfa22a7fd31e4b92eaf4cbf9a72c5b5767b013b"),
            List.of("a", "b", "c"),
            List.of(Map.of(), Map.of(), Map.of()));

        int deleted = repo1024.delete(TENANT_A, col, List.of("e671f06a02abb1132e8ca202221695264e2f8118d3128ab13e29fa04c610584b", "47e66b7e0eb19e987de506efaea585e504734c8422d39fab7d247551a9da5c0d", "b7a3b4af75b715d9bdd02c2bf191e75cbf5464705760b87d28d4da178b311cc3"));
        assertThat(deleted).as("exactly the 2 existing ids deleted").isEqualTo(2);
        assertThat(repo1024.count(TENANT_A, col)).isEqualTo(1);
    }

    @Test
    void delete_emptyIds_isNoop() {
        String col = "code__deleteempty__voyage-code-3__v1";
        repo1024.upsertChunks(TENANT_A, col,
            List.of("06d06fa4d1601fefe2de63135a2e3b31d42c8326ab9fd706884819f8e8ba5b7e"), List.of("33932907ab2046a6cf049d7cae3a30d8246a51d402b90ab29355beba4558c203"), List.of(Map.of()));

        int deleted = repo1024.delete(TENANT_A, col, List.of());
        assertThat(deleted).as("empty ids list must delete exactly 0 rows").isEqualTo(0);
        assertThat(repo1024.count(TENANT_A, col)).isEqualTo(1);
    }

    @Test
    void delete_crossTenant_affectsZero_rowSurvives() {
        String col = "code__deleterls__voyage-code-3__v1";
        repo1024.upsertChunks(TENANT_A, col,
            List.of("74a452f909cd06c8f210eeb4f7d95fcaaca02d0def334ccb4330c72b1ca0f5f2"), List.of("33932907ab2046a6cf049d7cae3a30d8246a51d402b90ab29355beba4558c203"), List.of(Map.of()));

        int deleted = repo1024.delete(TENANT_B, col, List.of("74a452f909cd06c8f210eeb4f7d95fcaaca02d0def334ccb4330c72b1ca0f5f2"));
        assertThat(deleted)
            .as("cross-tenant delete through the repository must affect exactly 0 rows")
            .isEqualTo(0);
        assertThat(repo1024.count(TENANT_A, col))
            .as("the row must survive for its owner")
            .isEqualTo(1);
    }

    // ---------------------------------------------------------------------------
    // Contract 6b: selectExistingChashes (RDR-181 embed-skip existence lookup,
    // bead nexus-f0r8p.1) — a PK-indexed (collection, chash) existence probe.
    // ---------------------------------------------------------------------------

    @Test
    void selectExistingChashes_returnsExactlyThePresentSubset() throws Exception {
        String col = "code__existsk__voyage-code-3__v1";
        String existingChash = "c2f1158510b4e285e19115725c74e9e22ab93bc83bcb31fcd7160f1efe458f9b";
        repo1024.upsertChunks(TENANT_A, col,
            List.of(existingChash), List.of("existence probe text"), List.of(Map.of()));

        Set<String> present = repo1024.selectExistingChashes(TENANT_A, col,
            List.of(existingChash, "5beca5a7af6d3079d536d3af0ec4c15164611c88e57176b037a9418a471efa80"));

        assertThat(present)
            .as("only the chash with a stored row is reported present")
            .containsExactly(existingChash);
    }

    // ---------------------------------------------------------------------------
    // Contract 7: update-metadata (metadata only — no re-embed, text unchanged)
    // ---------------------------------------------------------------------------

    @Test
    void updateMetadata_replacesMetadata_textAndEmbeddingUntouched() throws Exception {
        String col = "code__updatemeta__voyage-code-3__v1";
        embedder1024.register("frecency text", 0.6f, 0.8f);
        repo1024.upsertChunks(TENANT_A, col,
            List.of("7a52d407bbf717ca55a9ca6b0f65b28821febc86d097b8275e4157a2fc2b4dc9"), List.of("frecency text"), List.of(Map.of("frecency_score", "0.1")));

        repo1024.updateMetadata(TENANT_A, col,
            List.of("7a52d407bbf717ca55a9ca6b0f65b28821febc86d097b8275e4157a2fc2b4dc9"), List.of(Map.of("frecency_score", "0.9")));

        Map<String, Object> got = repo1024.get(TENANT_A, col, List.of("7a52d407bbf717ca55a9ca6b0f65b28821febc86d097b8275e4157a2fc2b4dc9"), 10, 0);
        @SuppressWarnings("unchecked")
        List<Map<String, Object>> metas = (List<Map<String, Object>>) got.get("metadatas");
        @SuppressWarnings("unchecked")
        List<String> docs = (List<String>) got.get("documents");
        assertThat(metas.get(0).get("frecency_score"))
            .as("metadata must be replaced")
            .isEqualTo("0.9");
        assertThat(docs.get(0))
            .as("chunk_text must be untouched by a metadata-only update")
            .isEqualTo("frecency text");

        double distToOriginal = superuserCosineDistance(
            1024, col, "7a52d407bbf717ca55a9ca6b0f65b28821febc86d097b8275e4157a2fc2b4dc9", FakeEmbedder.unitVector(1024, 0.6f, 0.8f));
        assertThat(distToOriginal)
            .as("embedding must be untouched by a metadata-only update (no re-embed)")
            .isCloseTo(0.0, within(1e-6));
    }

    @Test
    void updateMetadata_misalignedIdsAndMetadatas_failsLoud() {
        String col = "code__updatemeta-align__voyage-code-3__v1";
        assertThatThrownBy(() ->
            repo1024.updateMetadata(TENANT_A, col,
                List.of("8a25f1a1e3db58c58676b50023f2079a0f98befefcb51ebe267578be4aa77c08", "ef638debeb5738ed8ee3828e052a501d2b478657f49b70565482bf2f55129a67"), List.of(Map.of("v", "99868a64d5c07efffca22fdec8f537ea946fe34f5b91e21e2cd75193dfe2f239"))))
            .as("ids and metadatas of different sizes must fail loud, not zip-truncate")
            .isInstanceOf(IllegalArgumentException.class);
    }

    @Test
    void updateMetadata_crossTenant_noEffect() {
        String col = "code__updatemeta-rls__voyage-code-3__v1";
        repo1024.upsertChunks(TENANT_A, col,
            List.of("b87a8c2c61b0a1391df30e59598767aa8291f3a6ea89ef8d77b367eb27efa804"), List.of("owned text"), List.of(Map.of("v", "e9941e196dc41bcf2e7288f58b96ac6a33041c5d7b3955f3c3db25869510ddad")));

        // tenant-b attempts to overwrite tenant-a's metadata: RLS makes the row
        // invisible, so the update affects nothing (frecency path, nexus-enehl —
        // cross-tenant frecency corruption would be silent and persistent).
        repo1024.updateMetadata(TENANT_B, col,
            List.of("b87a8c2c61b0a1391df30e59598767aa8291f3a6ea89ef8d77b367eb27efa804"), List.of(Map.of("v", "corrupted")));

        Map<String, Object> got = repo1024.get(TENANT_A, col, List.of("b87a8c2c61b0a1391df30e59598767aa8291f3a6ea89ef8d77b367eb27efa804"), 10, 0);
        @SuppressWarnings("unchecked")
        List<Map<String, Object>> metas = (List<Map<String, Object>>) got.get("metadatas");
        assertThat(metas).hasSize(1);
        assertThat(metas.get(0).get("v"))
            .as("cross-tenant updateMetadata must not modify the owning tenant's metadata")
            .isEqualTo("e9941e196dc41bcf2e7288f58b96ac6a33041c5d7b3955f3c3db25869510ddad");
    }

    // ---------------------------------------------------------------------------
    // Contract 8: RDR-108 manifest join (documents.tumbler →
    // catalog_document_chunks(collection, chash) → chunks_<dim>)
    // ---------------------------------------------------------------------------

    @Test
    void manifestJoin_resolvesPositionOrderedChunks() throws Exception {
        String col = "code__manifest__voyage-code-3__v1";
        String tumbler = "1.9.1";
        repo1024.upsertChunks(TENANT_A, col,
            List.of("4db505d803940fb4bf6a7733d520e048e92624abe0f6df2621d755e0242d7fa8", "5371e5a9ebdbb7919a310c17fa77f7472eabf931d18d2c88635dd78caf6385ce", "ae30d428bacce3b3d1c2d182a36460a27e4fa9cc71276e93a33e985f87fea900"),
            List.of("manifest chunk one", "manifest chunk two", "manifest chunk three"),
            List.of(Map.of(), Map.of(), Map.of()));
        seedCatalogDocument(TENANT_A, tumbler, "Manifest Doc");
        // Insert manifest rows deliberately out of position order: the JOIN must
        // return position order, not insertion order.
        seedManifestRow(TENANT_A, tumbler, 2, "ae30d428bacce3b3d1c2d182a36460a27e4fa9cc71276e93a33e985f87fea900", col);
        seedManifestRow(TENANT_A, tumbler, 0, "4db505d803940fb4bf6a7733d520e048e92624abe0f6df2621d755e0242d7fa8", col);
        seedManifestRow(TENANT_A, tumbler, 1, "5371e5a9ebdbb7919a310c17fa77f7472eabf931d18d2c88635dd78caf6385ce", col);

        List<Map<String, Object>> rows = repo1024.fetchDocumentChunks(TENANT_A, tumbler);

        assertThat(rows).as("exactly the 3 manifest positions").hasSize(3);
        assertThat(rows).extracting(r -> ((Number) r.get("position")).intValue())
            .as("rows ordered by manifest position")
            .containsExactly(0, 1, 2);
        assertThat(rows).extracting(r -> r.get("chash"))
            .containsExactly("4db505d803940fb4bf6a7733d520e048e92624abe0f6df2621d755e0242d7fa8", "5371e5a9ebdbb7919a310c17fa77f7472eabf931d18d2c88635dd78caf6385ce", "ae30d428bacce3b3d1c2d182a36460a27e4fa9cc71276e93a33e985f87fea900");
        assertThat(rows).extracting(r -> r.get("chunk_text"))
            .as("chunk text resolved in-database from the dispatched chunks table")
            .containsExactly("manifest chunk one", "manifest chunk two", "manifest chunk three");
        assertThat(rows).extracting(r -> r.get("collection"))
            .containsExactly(col, col, col);
    }

    @Test
    void manifestJoin_sharedChash_returnsOneRowPerPosition() throws Exception {
        String col = "code__manifestshared__voyage-code-3__v1";
        String tumbler = "1.9.2";
        repo1024.upsertChunks(TENANT_A, col,
            List.of("c2aa14081f2549e385969d2e24430e49ce0ed820610624c15083bf3bc63b1c52"), List.of("repeated chunk text"), List.of(Map.of()));
        seedCatalogDocument(TENANT_A, tumbler, "Shared Chash Doc");
        // Two positions point at the SAME chash: identical text collapses to one chunk
        // row by design; the manifest preserves position (CLAUDE.md §Catalog/T3 split).
        seedManifestRow(TENANT_A, tumbler, 0, "c2aa14081f2549e385969d2e24430e49ce0ed820610624c15083bf3bc63b1c52", col);
        seedManifestRow(TENANT_A, tumbler, 1, "c2aa14081f2549e385969d2e24430e49ce0ed820610624c15083bf3bc63b1c52", col);

        List<Map<String, Object>> rows = repo1024.fetchDocumentChunks(TENANT_A, tumbler);

        assertThat(rows).as("one row per manifest position, even for a shared chash").hasSize(2);
        assertThat(rows).extracting(r -> ((Number) r.get("position")).intValue())
            .containsExactly(0, 1);
        assertThat(rows).extracting(r -> r.get("chunk_text"))
            .containsExactly("repeated chunk text", "repeated chunk text");
    }

    @Test
    void manifestJoin_emptyManifest_returnsEmpty_unlikeUnknownTumbler() throws Exception {
        // Boundary: a KNOWN tumbler with zero manifest rows (registered before chunking,
        // or all chunks purged) returns an empty list; an UNKNOWN tumbler throws. A
        // refactor collapsing the two cases would either leak fail-loud onto a valid
        // operational state or silently empty out unknown documents.
        String tumbler = "1.9.5";
        seedCatalogDocument(TENANT_A, tumbler, "Empty Manifest Doc");

        List<Map<String, Object>> rows = repo1024.fetchDocumentChunks(TENANT_A, tumbler);
        assertThat(rows)
            .as("a visible document with zero manifest rows yields an empty chunk list")
            .isEmpty();
    }

    @Test
    void manifestJoin_unresolvableChash_failsLoud() throws Exception {
        String col = "code__manifestbroken__voyage-code-3__v1";
        String tumbler = "1.9.3";
        repo1024.upsertChunks(TENANT_A, col,
            List.of("b460b20af2d6236d5932c51c4eb1206ec499e868ee10250db8363dd15baa848f"), List.of("resolvable chunk"), List.of(Map.of()));
        seedCatalogDocument(TENANT_A, tumbler, "Broken Manifest Doc");
        seedManifestRow(TENANT_A, tumbler, 0, "b460b20af2d6236d5932c51c4eb1206ec499e868ee10250db8363dd15baa848f", col);
        seedManifestRow(TENANT_A, tumbler, 1, "292001b2349cbd9c24bf8c243d4eb3eab82324f766669c5e0f6657c3296088bb", col);  // dangling chash

        assertThatThrownBy(() -> repo1024.fetchDocumentChunks(TENANT_A, tumbler))
            .as("a manifest row whose (collection, chash) has no chunk must fail loud — "
                + "a silently partial document is the no-silent-fallbacks hazard class")
            .isInstanceOf(IllegalStateException.class);
    }

    @Test
    void manifestJoin_unknownOrForeignTumbler_failsLoud() throws Exception {
        String col = "code__manifestrls__voyage-code-3__v1";
        String tumbler = "1.9.4";
        repo1024.upsertChunks(TENANT_A, col,
            List.of("f0e1580aedca3d2a91f7aec0414bbb14e87a62842ab79e8da79de0f8ca88fad8"), List.of("tenant a manifest chunk"), List.of(Map.of()));
        seedCatalogDocument(TENANT_A, tumbler, "RLS Manifest Doc");
        seedManifestRow(TENANT_A, tumbler, 0, "f0e1580aedca3d2a91f7aec0414bbb14e87a62842ab79e8da79de0f8ca88fad8", col);

        // tenant-b cannot see tenant-a's catalog document: same failure as an unknown
        // tumbler (RLS must not leak existence).
        assertThatThrownBy(() -> repo1024.fetchDocumentChunks(TENANT_B, tumbler))
            .as("a tumbler invisible under RLS must behave exactly like an unknown tumbler")
            .isInstanceOf(IllegalStateException.class);
        assertThatThrownBy(() -> repo1024.fetchDocumentChunks(TENANT_A, "9.9.9"))
            .as("an unknown tumbler must fail loud, not return an empty document")
            .isInstanceOf(IllegalStateException.class);
    }

    // ---------------------------------------------------------------------------
    // Helpers
    // ---------------------------------------------------------------------------

    /** Superuser row count for one collection in {@code nexus.chunks_<dim>} (bypasses RLS). */
    private long superuserCount(int dim, String collection) throws SQLException {
        try (Connection su = pg.createConnection("");
             PreparedStatement ps = su.prepareStatement(
                 "SELECT count(*) FROM nexus.chunks_" + dim + " WHERE collection = ?")) {
            ps.setString(1, collection);
            try (ResultSet rs = ps.executeQuery()) {
                rs.next();
                return rs.getLong(1);
            }
        }
    }

    /** Superuser fetch of one chunk's metadata as JSON text (bypasses RLS). */
    private String superuserChunkMetadataJson(int dim, String collection, String chash) throws SQLException {
        try (Connection su = pg.createConnection("");
             PreparedStatement ps = su.prepareStatement(
                 "SELECT metadata::text FROM nexus.chunks_" + dim
                 + " WHERE collection = ? AND chash = ?")) {
            ps.setString(1, collection);
            ps.setBytes(2, java.util.HexFormat.of().parseHex(chash));
            try (ResultSet rs = ps.executeQuery()) {
                assertThat(rs.next()).as("row %s/%s must exist in chunks_%d", collection, chash, dim).isTrue();
                return rs.getString(1);
            }
        }
    }

    /** Superuser fetch of one chunk's embedding as pgvector text (bypasses RLS). */
    private String superuserChunkEmbedding(int dim, String collection, String chash) throws SQLException {
        try (Connection su = pg.createConnection("");
             PreparedStatement ps = su.prepareStatement(
                 "SELECT embedding::text FROM nexus.chunks_" + dim
                 + " WHERE collection = ? AND chash = ?")) {
            ps.setString(1, collection);
            // chash is bytea(32) now (RDR-180) — bind the decoded digest, not hex text.
            ps.setBytes(2, java.util.HexFormat.of().parseHex(chash));
            try (ResultSet rs = ps.executeQuery()) {
                assertThat(rs.next()).as("row %s/%s must exist in chunks_%d", collection, chash, dim).isTrue();
                return rs.getString(1);
            }
        }
    }

    /** Superuser fetch of one chunk's text (bypasses RLS). */
    private String superuserChunkText(int dim, String collection, String chash) throws SQLException {
        try (Connection su = pg.createConnection("");
             PreparedStatement ps = su.prepareStatement(
                 "SELECT chunk_text FROM nexus.chunks_" + dim
                 + " WHERE collection = ? AND chash = ?")) {
            ps.setString(1, collection);
            ps.setBytes(2, java.util.HexFormat.of().parseHex(chash));
            try (ResultSet rs = ps.executeQuery()) {
                assertThat(rs.next()).as("row %s/%s must exist in chunks_%d", collection, chash, dim).isTrue();
                return rs.getString(1);
            }
        }
    }

    /** Superuser cosine distance between a stored embedding and an expected vector. */
    private double superuserCosineDistance(int dim, String collection, String chash,
                                           float[] expected) throws SQLException {
        StringBuilder lit = new StringBuilder("[");
        for (int i = 0; i < expected.length; i++) {
            if (i > 0) lit.append(',');
            lit.append(expected[i]);
        }
        lit.append(']');
        try (Connection su = pg.createConnection("");
             PreparedStatement ps = su.prepareStatement(
                 "SELECT embedding <=> ?::vector FROM nexus.chunks_" + dim
                 + " WHERE collection = ? AND chash = ?")) {
            ps.setString(1, lit.toString());
            ps.setString(2, collection);
            ps.setBytes(3, java.util.HexFormat.of().parseHex(chash));
            try (ResultSet rs = ps.executeQuery()) {
                assertThat(rs.next()).as("row %s/%s must exist in chunks_%d", collection, chash, dim).isTrue();
                return rs.getDouble(1);
            }
        }
    }

    /** Superuser insert of a catalog document (bypasses catalog RLS; tenant explicit). */
    private void seedCatalogDocument(String tenant, String tumbler, String title) throws SQLException {
        try (Connection su = pg.createConnection("");
             PreparedStatement ps = su.prepareStatement(
                 "INSERT INTO nexus.catalog_documents (tenant_id, tumbler, title) VALUES (?, ?, ?)")) {
            ps.setString(1, tenant);
            ps.setString(2, tumbler);
            ps.setString(3, title);
            ps.executeUpdate();
        }
    }

    /** Superuser insert of a manifest row with the RDR-155 {@code collection} column set. */
    private void seedManifestRow(String tenant, String docId, int position,
                                 String chash, String collection) throws SQLException {
        try (Connection su = pg.createConnection("");
             PreparedStatement ps = su.prepareStatement(
                 "INSERT INTO nexus.catalog_document_chunks "
                 + "(tenant_id, doc_id, position, chash, collection) VALUES (?, ?, ?, ?, ?)")) {
            ps.setString(1, tenant);
            ps.setString(2, docId);
            ps.setInt(3, position);
            // chash is bytea(32) now (RDR-180) — bind the decoded digest, not hex text.
            ps.setBytes(4, java.util.HexFormat.of().parseHex(chash));
            ps.setString(5, collection);
            ps.executeUpdate();
        }
    }

    /**
     * Deterministic test embedder: registered texts map to unit vectors confined to the
     * first two components, so pairwise cosine distances are exact and assertable.
     * Unregistered texts embed to the first basis vector (1, 0, 0, ...).
     */
    static final class FakeEmbedder implements Embedder {

        private final int dim;
        private final Map<String, float[]> registered = new HashMap<>();

        FakeEmbedder(int dim) {
            this.dim = dim;
        }

        /** Register {@code text} → unit vector ({@code x}, {@code y}, 0, ..., 0). */
        void register(String text, float x, float y) {
            registered.put(text, unitVector(dim, x, y));
        }

        /**
         * Build ({@code x}, {@code y}, 0, ..., 0). Enforces {@code x² + y² = 1}: the
         * suite's exact cosine-distance assertions are only valid for unit vectors, and
         * a non-unit pair would silently skew them.
         */
        static float[] unitVector(int dim, float x, float y) {
            if (Math.abs(x * x + y * y - 1.0f) > 1e-5f) {
                throw new IllegalArgumentException(
                    "(" + x + ", " + y + ") is not a unit vector — distance assertions would skew");
            }
            float[] v = new float[dim];
            v[0] = x;
            v[1] = y;
            return v;
        }

        @Override
        public List<float[]> embed(List<String> texts) {
            List<float[]> out = new ArrayList<>(texts.size());
            for (String t : texts) {
                float[] v = registered.getOrDefault(t, unitVector(dim, 1.0f, 0.0f));
                // Defensive copy: a caller normalizing in place must not corrupt the
                // registered vector for later embeds of the same text.
                out.add(java.util.Arrays.copyOf(v, v.length));
            }
            return out;
        }
    }
}
