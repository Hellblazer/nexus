// SPDX-License-Identifier: AGPL-3.0-or-later
package dev.nexus.service.vectors;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.PgContainerHelper;
import dev.nexus.service.db.Chash;
import dev.nexus.service.db.TenantScope;
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
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.sql.PreparedStatement;
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
 * <p>Fixture: one row written the normal way (via {@link
 * PgVectorRepository#upsertChunks}, dispatched at the collection's OWN dim, 1024
 * for {@code voyage-code-3}), plus one row inserted directly (superuser, bypasses
 * the repository's dim dispatch — the only way to construct the mixed-dim state
 * at all, since every application write path dispatches through {@code
 * dimForCollection}) under the SAME {@code (tenant, collection)} but with a
 * DIFFERENT chash and its {@code embedding_768} column populated instead of
 * {@code embedding_1024}. The {@code exactly_one_embedding} CHECK constraint
 * (vectors-004-unify-chunks.xml) guarantees this is one row with exactly one
 * populated embedding column, not two rows sharing one physical slot.
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
    private static final String CHASH_FOREIGN_DIM =
        Chash.ofText("dim-guard-foreign-dim-chunk").toHex();

    private PostgreSQLContainer<?> pg;
    private HikariDataSource svcDs;
    private TenantScope tenantScope;
    private PgVectorRepository repo;

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
                "    CREATE ROLE nexus_svc LOGIN PASSWORD 'nexus_svc_pass' NOSUPERUSER NOBYPASSRLS; " +
                "  END IF; " +
                "END $$");
        }
        try (Connection su = pg.createConnection("")) {
            Database db = DatabaseFactory.getInstance()
                .findCorrectDatabaseImplementation(new JdbcConnection(su));
            try (Liquibase liquibase = new Liquibase(
                    "db/changelog/db.changelog-master.xml",
                    new ClassLoaderResourceAccessor(), db)) {
                liquibase.update(new Contexts());
            }
        }
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            su.createStatement().execute("GRANT USAGE ON SCHEMA nexus TO " + SVC_ROLE);
            su.createStatement().execute(
                "GRANT SELECT, INSERT, UPDATE, DELETE ON " + DimTables.CHUNKS_TABLE_NAME + " TO " + SVC_ROLE);
            su.createStatement().execute(
                "GRANT SELECT, INSERT ON nexus.catalog_collections TO " + SVC_ROLE);
            su.createStatement().execute(
                "GRANT SELECT ON nexus.catalog_document_chunks, nexus.catalog_documents TO " + SVC_ROLE);
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

        var embedder = new ZeroEmbedder(1024);
        repo = new PgVectorRepository(tenantScope, embedder, embedder);

        // Own-dim row via the real application write path — also registers
        // nexus.catalog_collections for COLLECTION, satisfying the FK the
        // foreign-dim raw insert below relies on.
        repo.upsertChunks(TENANT, COLLECTION,
            List.of(CHASH_OWN_DIM), List.of("own-dim chunk text"), List.of(Map.of()));

        // Foreign-dim row: same (tenant, collection), a DISTINCT chash, but
        // embedding_768 populated instead of embedding_1024 — the only way to
        // construct this state, since every application write path dispatches
        // through dimForCollection and would never write embedding_768 under a
        // 1024-dispatched collection name. Superuser bypasses RLS, same
        // convention PgVectorTombstoneFilterTest uses for out-of-band fixture rows.
        try (Connection su = pg.createConnection("");
             PreparedStatement ps = su.prepareStatement(
                 "INSERT INTO nexus.chunks "
                 + "(tenant_id, collection, chash, chunk_text, embedding_768, metadata, created_at) "
                 + "VALUES (?, ?, decode(?, 'hex'), ?, ?::vector, '{}'::jsonb, now())")) {
            su.setAutoCommit(true);
            ps.setString(1, TENANT);
            ps.setString(2, COLLECTION);
            ps.setString(3, CHASH_FOREIGN_DIM);
            ps.setString(4, "foreign-dim chunk text");
            ps.setString(5, zeroVectorLiteral(768));
            ps.execute();
        }
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
        if (pg    != null) pg.stop();
    }

    private static String zeroVectorLiteral(int dim) {
        StringBuilder sb = new StringBuilder("[");
        for (int i = 0; i < dim; i++) {
            if (i > 0) sb.append(',');
            sb.append('0');
        }
        return sb.append(']').toString();
    }

    /**
     * The load-bearing case: a foreign-dim row must be OMITTED from the
     * envelope entirely, matching the Chroma-parity "ids not present are
     * omitted" contract this method's own javadoc already documents — NOT
     * present with an empty embedding list, which is what the unguarded
     * {@code ch.chash().in(ids)}-only predicate produced (the row matches the
     * key, {@code rec.value2()} is null for the un-dispatched column, and the
     * hydration loop stored an empty list rather than skipping the id).
     */
    @Test
    void getEmbeddings_foreignDimRow_omittedEntirely_notEmptyList() {
        var envelope = repo.getEmbeddings(TENANT, COLLECTION,
            List.of(CHASH_OWN_DIM, CHASH_FOREIGN_DIM));

        @SuppressWarnings("unchecked")
        List<String> ids = (List<String>) envelope.get("ids");
        @SuppressWarnings("unchecked")
        List<List<Float>> embeddings = (List<List<Float>>) envelope.get("embeddings");

        assertThat(ids)
            .as("foreign-dim chash must be OMITTED, not returned with an empty embedding")
            .containsExactly(CHASH_OWN_DIM)
            .doesNotContain(CHASH_FOREIGN_DIM);
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
     * never hits the zero-count branch" invariant. This is DELIBERATE cross-endpoint
     * coherence, not an unguarded accident left over from before nexus-oizh7 — the
     * mixed-dim fixture's own-dim row (embedding_1024) AND foreign-dim row
     * (embedding_768) BOTH count, exactly as {@link TaxonomyCentroidRepository#count}
     * already decided for the analogous centroid case ("counts centroids, not
     * centroids-at-a-dim").
     */
    @Test
    void count_isDimAgnostic_countsOwnDimAndForeignDimRows() {
        int c = repo.count(TENANT, COLLECTION);

        assertThat(c)
            .as("count must be dim-agnostic (collection total), counting BOTH the "
                + "own-dim (embedding_1024) row AND the foreign-dim (embedding_768) "
                + "row -- must agree with stats/list_collections' cross-dim sum")
            .isEqualTo(2);
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
}
