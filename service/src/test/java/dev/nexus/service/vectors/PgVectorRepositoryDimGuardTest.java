// SPDX-License-Identifier: AGPL-3.0-or-later
package dev.nexus.service.vectors;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.PgContainerHelper;
import dev.nexus.service.db.Chash;
import dev.nexus.service.db.TenantScope;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.util.List;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * Bead nexus-oizh7 (RDR-191 Phase 4 unification, D1-hazard class): {@link
 * PgVectorRepository#getEmbeddings} and {@link PgVectorRepository#count} were the
 * two remaining unguarded call sites keying purely on {@code collection} against
 * the now-unified {@code nexus.chunks} table with no {@code embedding_<dim> IS NOT
 * NULL} predicate.
 *
 * <p>Pre-unification a row could only physically exist in ONE of the three
 * per-dim {@code chunks_384}/{@code chunks_768}/{@code chunks_1024} tables, so table
 * membership alone WAS the dim filter (exactly {@code TaxonomyCentroidRepository}'s
 * D1-hazard class, and {@code CatalogRepository#strandedChunkCount}'s comment on the
 * same hazard for chunks). Post-unification every dim's rows live in the SAME
 * physical table, keyed only by {@code (tenant_id, collection, chash)} — a row
 * whose embedding lives in a DIFFERENT dim column now matches a collection-only
 * predicate even though it is not of that collection's dispatched dim. A
 * collection CAN legitimately hold rows at two dims at once mid-migration
 * (mirrors {@link TaxonomyCentroidRepository}'s own documented centroid-side
 * stance, that class's {@code search}/{@code count} javadoc), so this is a real
 * reachable state, not just a corrupted-data hypothetical.
 *
 * <p><b>RDR-225 (nexus-3wh8d.13):</b> {@code nexus.chunks} is partitioned by embedding model and a collection
 * has exactly ONE model, so one dimension. A model partition carries a CHECK that its vector column is the only
 * non-null one, so the mixed-dimension state this class used to build (a foreign-dimension row beside an
 * own-dimension row) cannot be represented, and legacy data of that shape is not supported. The seed and the
 * assertions that excluded a foreign-dimension row are gone (they could no longer fail); the refusal itself is
 * pinned by {@code P225MigrationWalkIntegrationTest}. What remains is the behaviour of the guarded read paths
 * over the only state a collection can hold, and the dense-gate test, which needs two gate matches to leave the
 * selective branch.
 *
 * <p>Real Postgres round trip (Testcontainers pgvector/pgvector:pg17), same
 * fixture convention as {@code PgVectorRepositoryGetAllMetadataCapBoundaryTest}.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class PgVectorRepositoryDimGuardTest {

    private static final String SVC_ROLE = "svc_dim_guard_test";
    private static final String SVC_PASS = "svc_dim_guard_test_pass";
    private static final String TENANT   = "dim-guard-tenant";

    // voyage-code-3 -> dim 1024 (PgVectorRepository.dimForCollection).
    private static final String COLLECTION = "code__dimguard__voyage-code-3__v1";

    private static final String CHASH_OWN_DIM =
        Chash.ofText("dim-guard-own-dim-chunk").toHex();
    /** Never inserted: a chash the collection does not hold. */
    private static final String CHASH_ABSENT =
        Chash.ofText("dim-guard-absent-chunk").toHex();

    // Second, dedicated collection + fixture for the nexus-74zvm NULL-distance guard
    // tests below (search/hybridSearch) -- these need WELL-DEFINED (non-zero-norm)
    // vectors, since pgvector's <=> cosine-distance operator errors on a zero vector,
    // which the COLLECTION fixture above deliberately uses (ZeroEmbedder) for its own
    // getEmbeddings/count tests that never touch <=>.
    private static final String COLLECTION_NULLGUARD = "code__dimguard2__voyage-code-3__v1";
    private static final String CHASH_NULLGUARD_OWN =
        Chash.ofText("dim-guard-nullguard-own-dim-chunk").toHex();
    private static final String CHASH_NULLGUARD_OWN_2 =
        Chash.ofText("dim-guard-nullguard-own-dim-chunk-two").toHex();

    private PostgreSQLContainer<?> pg;
    private HikariDataSource svcDs;
    private TenantScope tenantScope;
    private PgVectorRepository repo;
    /** Backed by {@link UnitAxisEmbedder} -- for the nexus-74zvm search/hybridSearch tests. */
    private PgVectorRepository repoNullGuard;

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();

        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.bootstrapServiceRole(su, SVC_ROLE, SVC_PASS);
        }

        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(SVC_ROLE);
        cfg.setPassword(SVC_PASS);
        cfg.setMaximumPoolSize(5);
        cfg.setAutoCommit(true);
        svcDs = new HikariDataSource(cfg);
        tenantScope = new TenantScope(svcDs);

        var embedder = new ZeroEmbedder(1024);
        repo = new PgVectorRepository(tenantScope, embedder, embedder);

        // RDR-204 Phase 1 (bead nexus-ft04v.7): chunks_collection_fk is a REAL,
        // always-enforced FK now -- PgVectorRepository's stub-insert is retired, so
        // the comment below is no longer true: the write path itself no longer
        // registers the collection, this must happen explicitly first.
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.insertCollection(
                org.jooq.impl.DSL.using(su, org.jooq.SQLDialect.POSTGRES), TENANT, COLLECTION);
        }
        // Own-dim row via the real application write path.
        repo.upsertChunks(TENANT, COLLECTION,
            List.of(CHASH_OWN_DIM), List.of("own-dim chunk text"), List.of(Map.of()));

        // RDR-192 Step 5 (nexus-wbfpw.10): getEmbeddings/list require a live own-collection manifest owner.
        own(COLLECTION, CHASH_OWN_DIM);

        // nexus-74zvm fixture: a SECOND collection, with well-defined (unit, non-zero-norm) vectors so
        // search/hybridSearch's <=> cosine-distance operator does not error.
        var unitEmbedder = new UnitAxisEmbedder(1024);
        repoNullGuard = new PgVectorRepository(tenantScope, unitEmbedder, unitEmbedder);
        // RDR-204 Phase 1 (bead nexus-ft04v.7): chunks_collection_fk is a REAL,
        // always-enforced FK now -- PgVectorRepository's stub-insert is retired.
        try (Connection su2 = pg.createConnection("")) {
            PgContainerHelper.insertCollection(
                org.jooq.impl.DSL.using(su2, org.jooq.SQLDialect.POSTGRES), TENANT, COLLECTION_NULLGUARD);
        }
        repoNullGuard.upsertChunks(TENANT, COLLECTION_NULLGUARD,
            List.of(CHASH_NULLGUARD_OWN), List.of("own dim ng chunk text"), List.of(Map.of()));
        // A second row, so the dense-gate test's selectiveGateMax=1 is exceeded.
        repoNullGuard.upsertChunks(TENANT, COLLECTION_NULLGUARD,
            List.of(CHASH_NULLGUARD_OWN_2), List.of("own dim ng chunk text two"), List.of(Map.of()));
        // RDR-192 Step 5 (nexus-wbfpw.10): search/hybridSearch require a live own-collection manifest owner too.
        own(COLLECTION_NULLGUARD, CHASH_NULLGUARD_OWN, CHASH_NULLGUARD_OWN_2);
    }

    /**
     * Give {@code ids} a live owner in {@code collection} for {@code TENANT}
     * (RDR-192 Step 5, bead nexus-wbfpw.10).
     */
    private void own(String collection, String... ids) {
        tenantScope.withTenant(TENANT, ctx -> {
            PgContainerHelper.ownChunks(ctx, TENANT, collection, ids);
            return null;
        });
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
        if (pg    != null) pg.stop();
    }

    /**
     * An id the collection does not hold is OMITTED from the envelope entirely, matching the Chroma-parity
     * "ids not present are omitted" contract this method's own javadoc documents, not returned with an empty
     * embedding list.
     */
    @Test
    void getEmbeddings_absentChash_omittedEntirely_notEmptyList() {
        var envelope = repo.getEmbeddings(TENANT, COLLECTION, List.of(CHASH_OWN_DIM, CHASH_ABSENT));

        @SuppressWarnings("unchecked")
        List<String> ids = (List<String>) envelope.get("ids");
        @SuppressWarnings("unchecked")
        List<List<Float>> embeddings = (List<List<Float>>) envelope.get("embeddings");

        assertThat(ids).containsExactly(CHASH_OWN_DIM);
        assertThat(embeddings).hasSize(1);
        assertThat(embeddings.get(0)).hasSize(1024);
    }

    /**
     * DECIDED semantics (nexus-hz89h, reversing the original nexus-oizh7 count()
     * guard as a CRITICAL regression — T2
     * {@code nexus/critique-nexus-oizh7-dim-guard-count-cross-endpoint-break.md}
     * [22539]): {@code count()} is deliberately DIM-AGNOSTIC — a collection total
     * across all dims, matching {@code GET /v1/vectors/stats}'s dim-summed
     * {@code list_collections()} total for the same name and preserving {@code
     * HttpVectorClient._count_or_key_error}'s "a list_collections-enumerated name
     * never hits the zero-count branch" invariant, exactly as {@link
     * TaxonomyCentroidRepository#count} decided for centroids. Since RDR-225 a collection
     * holds one dimension, so the total is that dimension's rows.
     */
    @Test
    void count_isTheCollectionTotal() {
        assertThat(repo.count(TENANT, COLLECTION)).isEqualTo(1);
    }

    /**
     * DECIDED semantics (nexus-3rprg, completing the nexus-hz89h/{@code count()} line for
     * the rest of the metadata-read family): {@code list()} is deliberately DIM-AGNOSTIC,
     * same as {@code count()} -- a chunk's text/metadata is collection content regardless
     * of which dim's embedding column it populates. See {@link PgVectorRepository
     * #dimForCollection}'s DECISION for the anchor statement of this contract.
     */
    @Test
    void list_isTheCollectionMembership() {
        var envelope = repo.list(TENANT, COLLECTION, 100, 0);

        @SuppressWarnings("unchecked")
        List<String> ids = (List<String>) envelope.get("ids");

        assertThat(ids).containsExactlyInAnyOrder(CHASH_OWN_DIM);
    }

    /**
     * nexus-74zvm: {@link PgVectorRepository#search} returns the live own-dim rows, each with a real
     * (non-null) distance. (The foreign-dimension row this once also asserted absent cannot exist since
     * RDR-225, so the exclusion itself is no longer exercised by data.)
     */
    @Test
    void search_returnsTheOwnDimRows_eachWithARealDistance() {
        var results = repoNullGuard.search(
            TENANT, "query text", List.of(COLLECTION_NULLGUARD), 10, null);

        assertThat(results).extracting(r -> r.get("id"))
            .containsExactlyInAnyOrder(CHASH_NULLGUARD_OWN, CHASH_NULLGUARD_OWN_2);
        assertThat(results).allSatisfy(r -> assertThat(r.get("distance")).isNotNull());
    }

    /** nexus-74zvm, hybridSearch companion (selective-gate branch: two gate matches, far under SELECTIVE_GATE_MAX). */
    @Test
    void hybridSearch_returnsTheOwnDimRows_eachWithARealDistance() {
        var results = repoNullGuard.hybridSearch(
            TENANT, "chunk text", List.of(COLLECTION_NULLGUARD), 10, null);

        assertThat(results).extracting(r -> r.get("id"))
            .containsExactlyInAnyOrder(CHASH_NULLGUARD_OWN, CHASH_NULLGUARD_OWN_2);
        assertThat(results).allSatisfy(r -> assertThat(r.get("distance")).isNotNull());
    }

    /**
     * The dense-gate (HNSW-first) branch of hybridSearch, forced through the public 6-arg overload with
     * selectiveGateMax=1: the fixture's two gate matches exceed it, so the selective-chash-IN branch is
     * skipped. Its plan shape is proven by {@code PgVectorRepositoryRawSqlPlanShapeTest}; this shows the
     * branch returns the same rows with real distances.
     */
    @Test
    void hybridSearch_denseGateBranch_returnsTheOwnDimRows_eachWithARealDistance() {
        var results = repoNullGuard.hybridSearch(
            TENANT, "chunk text", List.of(COLLECTION_NULLGUARD), 10, null, 1);

        assertThat(results).extracting(r -> r.get("id"))
            .containsExactlyInAnyOrder(CHASH_NULLGUARD_OWN, CHASH_NULLGUARD_OWN_2);
        assertThat(results).allSatisfy(r -> assertThat(r.get("distance")).isNotNull());
    }

    private static final class ZeroEmbedder implements Embedder {
        private final int dim;

        ZeroEmbedder(int dim) {
            this.dim = dim;
        }

        @Override
        public List<float[]> embed(List<String> texts) {
            List<float[]> out = new java.util.ArrayList<>(texts.size());
            for (String ignored : texts) out.add(new float[dim]);
            return out;
        }
    }

    /** {@code [1,0,0,...,0]} for every text -- well-defined (non-zero-norm) query vectors. */
    private static final class UnitAxisEmbedder implements Embedder {
        private final int dim;

        UnitAxisEmbedder(int dim) {
            this.dim = dim;
        }

        @Override
        public List<float[]> embed(List<String> texts) {
            List<float[]> out = new java.util.ArrayList<>(texts.size());
            for (String ignored : texts) {
                float[] v = new float[dim];
                v[0] = 1.0f;
                out.add(v);
            }
            return out;
        }
    }
}
