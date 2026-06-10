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
            List.of("disp-1024-c1", "disp-1024-c2"),
            List.of("dispatch text one", "dispatch text two"),
            List.of(Map.of("kind", "d"), Map.of("kind", "d")));

        assertThat(superuserCount(1024, col)).as("both rows in chunks_1024").isEqualTo(2L);
        assertThat(superuserCount(768,  col)).as("nothing in chunks_768").isEqualTo(0L);
        assertThat(superuserCount(384,  col)).as("nothing in chunks_384").isEqualTo(0L);
    }

    @Test
    void upsert_voyage3_landsInChunks1024Only() throws Exception {
        String col = "knowledge__v3disp__voyage-3__v1";
        repo1024.upsertChunks(TENANT_A, col,
            List.of("disp-v3-c1"),
            List.of("voyage-3 dispatch text"),
            List.of(Map.of("kind", "d")));

        assertThat(superuserCount(1024, col)).as("row in chunks_1024").isEqualTo(1L);
        assertThat(superuserCount(768,  col)).as("nothing in chunks_768").isEqualTo(0L);
        assertThat(superuserCount(384,  col)).as("nothing in chunks_384").isEqualTo(0L);
    }

    @Test
    void upsert_bge768_landsInChunks768Only() throws Exception {
        String col = COL_BGE_768;
        repo768.upsertChunks(TENANT_A, col,
            List.of("disp-768-c1"),
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
            List.of("disp-384-c1"),
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
                List.of("unk-c1"), List.of("unknown model text"), List.of(Map.of())))
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
                List.of("mis-c1"), List.of("mismatched vector"), List.of(Map.of())))
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
            List.of("exact-c1"), List.of("exact vector text"), List.of(Map.of()));

        double distToExpected = superuserCosineDistance(
            1024, col, "exact-c1", FakeEmbedder.unitVector(1024, 0.6f, 0.8f));
        assertThat(distToExpected)
            .as("stored embedding must be exactly the embedder's output (cosine distance 0)")
            .isCloseTo(0.0, within(1e-6));
    }

    @Test
    void upsert_duplicateIdsInBatch_firstWins() throws Exception {
        String col = "code__dedup__voyage-code-3__v1";
        repo1024.upsertChunks(TENANT_A, col,
            List.of("dup-c1", "dup-c1"),
            List.of("first occurrence", "second occurrence"),
            List.of(Map.of("ord", "first"), Map.of("ord", "second")));

        assertThat(superuserCount(1024, col))
            .as("duplicate IDs in one batch must collapse to exactly 1 row")
            .isEqualTo(1L);
        assertThat(superuserChunkText(1024, col, "dup-c1"))
            .as("first-wins: the surviving row carries the FIRST text (T3Database._write_batch semantics)")
            .isEqualTo("first occurrence");
    }

    @Test
    void upsert_reUpsertSameChash_updatesInPlace() throws Exception {
        String col = "code__reupsert__voyage-code-3__v1";
        embedder1024.register("version one", 1.0f, 0.0f);
        embedder1024.register("version two", 0.0f, 1.0f);   // orthogonal to version one
        repo1024.upsertChunks(TENANT_A, col,
            List.of("re-c1"), List.of("version one"), List.of(Map.of("rev", "1")));
        repo1024.upsertChunks(TENANT_A, col,
            List.of("re-c1"), List.of("version two"), List.of(Map.of("rev", "2")));

        assertThat(superuserCount(1024, col))
            .as("re-upsert of the same chash must not create a second row")
            .isEqualTo(1L);
        assertThat(superuserChunkText(1024, col, "re-c1"))
            .as("re-upsert must update chunk_text in place (Chroma upsert semantics)")
            .isEqualTo("version two");
        assertThat(superuserCosineDistance(1024, col, "re-c1",
                FakeEmbedder.unitVector(1024, 0.0f, 1.0f)))
            .as("re-upsert must re-embed: the stored vector matches the NEW text's "
                + "embedding, not the original")
            .isCloseTo(0.0, within(1e-6));

        Map<String, Object> got = repo1024.get(TENANT_A, col, List.of("re-c1"), 10, 0);
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
            List.of("attr-c1"), List.of("tenant a text"), List.of(Map.of()));

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
            List.of("s-near", "s-mid", "s-far"),
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
            .containsExactly("s-near", "s-mid", "s-far");
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
            .containsExactly("s-near", "s-far");
    }

    @Test
    void search_wherePredicate_multiKeyIsAnded() {
        String col = "code__searchand__voyage-code-3__v1";
        embedder1024.register("search query", 1.0f, 0.0f);
        embedder1024.register("and t1", 1.0f, 0.0f);
        embedder1024.register("and t2", 0.8f, 0.6f);
        embedder1024.register("and t3", 0.0f, 1.0f);
        repo1024.upsertChunks(TENANT_A, col,
            List.of("and-c1", "and-c2", "and-c3"),
            List.of("and t1", "and t2", "and t3"),
            List.of(Map.of("kind", "a", "score", "high"),
                    Map.of("kind", "a", "score", "low"),
                    Map.of("kind", "b", "score", "high")));

        List<Map<String, Object>> rows = repo1024.search(
            TENANT_A, "search query", List.of(col), 10,
            Map.of("kind", "a", "score", "high"));

        assertThat(rows).extracting(r -> r.get("id"))
            .as("multi-key where must AND all predicates: exactly the one row matching both")
            .containsExactly("and-c1");
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
            List.of("mc-xnear", "mc-xfar"), List.of("x near", "x far"),
            List.of(Map.of(), Map.of()));
        repo1024.upsertChunks(TENANT_A, colY,
            List.of("mc-ymid"), List.of("y mid"), List.of(Map.of()));

        List<Map<String, Object>> rows = repo1024.search(
            TENANT_A, "search query", List.of(colX, colY), 10, null);

        assertThat(rows).extracting(r -> r.get("id"))
            .as("multi-collection union must interleave by distance, not group by collection")
            .containsExactly("mc-xnear", "mc-ymid", "mc-xfar");
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
            .containsExactly("s-near", "s-mid");
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
            List.of("g-c1", "g-c2"),
            List.of("get text one", "get text two"),
            List.of(Map.of("m", "1"), Map.of("m", "2")));

        Map<String, Object> got = repo1024.get(
            TENANT_A, col, List.of("g-c1", "g-c2", "g-missing"), 10, 0);

        @SuppressWarnings("unchecked")
        List<String> ids = (List<String>) got.get("ids");
        @SuppressWarnings("unchecked")
        List<String> docs = (List<String>) got.get("documents");
        @SuppressWarnings("unchecked")
        List<Map<String, Object>> metas = (List<Map<String, Object>>) got.get("metadatas");

        assertThat(ids).as("missing id omitted, both present ids returned")
            .containsExactlyInAnyOrder("g-c1", "g-c2");
        assertThat(docs).hasSize(2);
        assertThat(metas).hasSize(2);
        int i1 = ids.indexOf("g-c1");
        assertThat(docs.get(i1)).isEqualTo("get text one");
        assertThat(metas.get(i1).get("m")).isEqualTo("1");
    }

    @Test
    void get_limitOffset_skipsInChashOrder() {
        String col = "code__getoffset__voyage-code-3__v1";
        // Insertion order is the REVERSE of chash order ("go-z9" > "go-a2"), so this
        // assertion can distinguish chash-order pagination from insertion-order or
        // id-list-order pagination: those would return "go-a2" at offset 1.
        repo1024.upsertChunks(TENANT_A, col,
            List.of("go-z9", "go-a2"),
            List.of("offset text z", "offset text a"),
            List.of(Map.of(), Map.of()));

        Map<String, Object> got = repo1024.get(
            TENANT_A, col, List.of("go-z9", "go-a2"), 1, 1);
        @SuppressWarnings("unchecked")
        List<String> ids = (List<String>) got.get("ids");

        assertThat(ids)
            .as("limit=1 offset=1 must skip the first chash ('go-a2') and return 'go-z9'")
            .containsExactly("go-z9");
    }

    @Test
    void get_crossTenant_returnsEmptyEnvelope() throws Exception {
        String col = "code__getrls__voyage-code-3__v1";
        repo1024.upsertChunks(TENANT_A, col,
            List.of("gr-c1"), List.of("tenant a only"), List.of(Map.of()));
        assertThat(superuserCount(1024, col))
            .as("control: tenant-a's row must physically exist before the RLS assertion")
            .isEqualTo(1L);

        Map<String, Object> got = repo1024.get(TENANT_B, col, List.of("gr-c1"), 10, 0);
        @SuppressWarnings("unchecked")
        List<String> ids = (List<String>) got.get("ids");
        assertThat(ids).as("tenant-b must get 0 of tenant-a's chunks").isEmpty();
    }

    @Test
    void list_paginatesWithLimitOffset_disjointAndComplete() {
        String col = "code__listpage__voyage-code-3__v1";
        List<String> allIds = List.of("l-c1", "l-c2", "l-c3", "l-c4", "l-c5");
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
            .containsExactly("l-c1", "l-c2", "l-c3");
        assertThat(page2).as("second page: exactly the remaining 2 chashes, in order")
            .containsExactly("l-c4", "l-c5");
        Set<String> union = new LinkedHashSet<>();
        union.addAll(page1);
        union.addAll(page2);
        assertThat(union)
            .as("pages are disjoint and together cover all 5 ids")
            .containsExactlyInAnyOrderElementsOf(allIds);
    }

    @Test
    void count_exactPerCollection_andZeroForOtherTenant() {
        String col = "code__countexact__voyage-code-3__v1";
        repo1024.upsertChunks(TENANT_A, col,
            List.of("cnt-c1", "cnt-c2", "cnt-c3"),
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
            List.of("d-c1", "d-c2", "d-c3"),
            List.of("a", "b", "c"),
            List.of(Map.of(), Map.of(), Map.of()));

        int deleted = repo1024.delete(TENANT_A, col, List.of("d-c1", "d-c2", "d-missing"));
        assertThat(deleted).as("exactly the 2 existing ids deleted").isEqualTo(2);
        assertThat(repo1024.count(TENANT_A, col)).isEqualTo(1);
    }

    @Test
    void delete_emptyIds_isNoop() {
        String col = "code__deleteempty__voyage-code-3__v1";
        repo1024.upsertChunks(TENANT_A, col,
            List.of("de-c1"), List.of("survivor"), List.of(Map.of()));

        int deleted = repo1024.delete(TENANT_A, col, List.of());
        assertThat(deleted).as("empty ids list must delete exactly 0 rows").isEqualTo(0);
        assertThat(repo1024.count(TENANT_A, col)).isEqualTo(1);
    }

    @Test
    void delete_crossTenant_affectsZero_rowSurvives() {
        String col = "code__deleterls__voyage-code-3__v1";
        repo1024.upsertChunks(TENANT_A, col,
            List.of("dr-c1"), List.of("survivor"), List.of(Map.of()));

        int deleted = repo1024.delete(TENANT_B, col, List.of("dr-c1"));
        assertThat(deleted)
            .as("cross-tenant delete through the repository must affect exactly 0 rows")
            .isEqualTo(0);
        assertThat(repo1024.count(TENANT_A, col))
            .as("the row must survive for its owner")
            .isEqualTo(1);
    }

    // ---------------------------------------------------------------------------
    // Contract 7: update-metadata (metadata only — no re-embed, text unchanged)
    // ---------------------------------------------------------------------------

    @Test
    void updateMetadata_replacesMetadata_textAndEmbeddingUntouched() throws Exception {
        String col = "code__updatemeta__voyage-code-3__v1";
        embedder1024.register("frecency text", 0.6f, 0.8f);
        repo1024.upsertChunks(TENANT_A, col,
            List.of("um-c1"), List.of("frecency text"), List.of(Map.of("frecency_score", "0.1")));

        repo1024.updateMetadata(TENANT_A, col,
            List.of("um-c1"), List.of(Map.of("frecency_score", "0.9")));

        Map<String, Object> got = repo1024.get(TENANT_A, col, List.of("um-c1"), 10, 0);
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
            1024, col, "um-c1", FakeEmbedder.unitVector(1024, 0.6f, 0.8f));
        assertThat(distToOriginal)
            .as("embedding must be untouched by a metadata-only update (no re-embed)")
            .isCloseTo(0.0, within(1e-6));
    }

    @Test
    void updateMetadata_crossTenant_noEffect() {
        String col = "code__updatemeta-rls__voyage-code-3__v1";
        repo1024.upsertChunks(TENANT_A, col,
            List.of("umr-c1"), List.of("owned text"), List.of(Map.of("v", "original")));

        // tenant-b attempts to overwrite tenant-a's metadata: RLS makes the row
        // invisible, so the update affects nothing (frecency path, nexus-enehl —
        // cross-tenant frecency corruption would be silent and persistent).
        repo1024.updateMetadata(TENANT_B, col,
            List.of("umr-c1"), List.of(Map.of("v", "corrupted")));

        Map<String, Object> got = repo1024.get(TENANT_A, col, List.of("umr-c1"), 10, 0);
        @SuppressWarnings("unchecked")
        List<Map<String, Object>> metas = (List<Map<String, Object>>) got.get("metadatas");
        assertThat(metas).hasSize(1);
        assertThat(metas.get(0).get("v"))
            .as("cross-tenant updateMetadata must not modify the owning tenant's metadata")
            .isEqualTo("original");
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
            List.of("mf-c1", "mf-c2", "mf-c3"),
            List.of("manifest chunk one", "manifest chunk two", "manifest chunk three"),
            List.of(Map.of(), Map.of(), Map.of()));
        seedCatalogDocument(TENANT_A, tumbler, "Manifest Doc");
        // Insert manifest rows deliberately out of position order: the JOIN must
        // return position order, not insertion order.
        seedManifestRow(TENANT_A, tumbler, 2, "mf-c3", col);
        seedManifestRow(TENANT_A, tumbler, 0, "mf-c1", col);
        seedManifestRow(TENANT_A, tumbler, 1, "mf-c2", col);

        List<Map<String, Object>> rows = repo1024.fetchDocumentChunks(TENANT_A, tumbler);

        assertThat(rows).as("exactly the 3 manifest positions").hasSize(3);
        assertThat(rows).extracting(r -> ((Number) r.get("position")).intValue())
            .as("rows ordered by manifest position")
            .containsExactly(0, 1, 2);
        assertThat(rows).extracting(r -> r.get("chash"))
            .containsExactly("mf-c1", "mf-c2", "mf-c3");
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
            List.of("mfs-c1"), List.of("repeated chunk text"), List.of(Map.of()));
        seedCatalogDocument(TENANT_A, tumbler, "Shared Chash Doc");
        // Two positions point at the SAME chash: identical text collapses to one chunk
        // row by design; the manifest preserves position (CLAUDE.md §Catalog/T3 split).
        seedManifestRow(TENANT_A, tumbler, 0, "mfs-c1", col);
        seedManifestRow(TENANT_A, tumbler, 1, "mfs-c1", col);

        List<Map<String, Object>> rows = repo1024.fetchDocumentChunks(TENANT_A, tumbler);

        assertThat(rows).as("one row per manifest position, even for a shared chash").hasSize(2);
        assertThat(rows).extracting(r -> ((Number) r.get("position")).intValue())
            .containsExactly(0, 1);
        assertThat(rows).extracting(r -> r.get("chunk_text"))
            .containsExactly("repeated chunk text", "repeated chunk text");
    }

    @Test
    void manifestJoin_unresolvableChash_failsLoud() throws Exception {
        String col = "code__manifestbroken__voyage-code-3__v1";
        String tumbler = "1.9.3";
        repo1024.upsertChunks(TENANT_A, col,
            List.of("mfb-c1"), List.of("resolvable chunk"), List.of(Map.of()));
        seedCatalogDocument(TENANT_A, tumbler, "Broken Manifest Doc");
        seedManifestRow(TENANT_A, tumbler, 0, "mfb-c1", col);
        seedManifestRow(TENANT_A, tumbler, 1, "mfb-missing", col);  // dangling chash

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
            List.of("mfr-c1"), List.of("tenant a manifest chunk"), List.of(Map.of()));
        seedCatalogDocument(TENANT_A, tumbler, "RLS Manifest Doc");
        seedManifestRow(TENANT_A, tumbler, 0, "mfr-c1", col);

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

    /** Superuser fetch of one chunk's text (bypasses RLS). */
    private String superuserChunkText(int dim, String collection, String chash) throws SQLException {
        try (Connection su = pg.createConnection("");
             PreparedStatement ps = su.prepareStatement(
                 "SELECT chunk_text FROM nexus.chunks_" + dim
                 + " WHERE collection = ? AND chash = ?")) {
            ps.setString(1, collection);
            ps.setString(2, chash);
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
            ps.setString(3, chash);
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
            ps.setString(4, chash);
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
