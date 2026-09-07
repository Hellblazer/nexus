// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import liquibase.Contexts;
import liquibase.LabelExpression;
import liquibase.Liquibase;
import liquibase.changelog.ChangeSet;
import liquibase.database.Database;
import liquibase.database.DatabaseFactory;
import liquibase.database.jvm.JdbcConnection;
import liquibase.resource.ClassLoaderResourceAccessor;
import org.jooq.DSLContext;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.Test;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.util.List;
import java.util.Map;
import java.util.stream.Collectors;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static dev.nexus.service.jooq.nexus.Tables.EMBEDDING_MODELS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-204 Phase 1 item 1 (bead nexus-ft04v.2) -- Liquibase schema-apply test
 * for {@code catalog-036-embedding-profile.xml}: {@code nexus.embedding_models}
 * (seeded, no RLS), {@code nexus.embedding_profile} (tenant-scoped, RLS, FK to
 * {@code embedding_models}), and the two new NULLABLE {@code catalog_collections}
 * columns ({@code dimension}, {@code lifecycle_state}) the backfill walk (bead
 * nexus-ft04v.4) will populate later.
 *
 * <p>The first four tests run against {@link PgContainerHelper#start()}'s
 * shared, already-fully-migrated cluster (the same idiom {@link
 * CatalogSchemaLiquibaseTest}/{@link TelemetrySchemaLiquibaseTest} use) --
 * that shared cluster is bootstrapped by one real full Liquibase walk from an
 * empty database (see {@link SharedCluster}), so shape/seed assertions
 * against it are also a live proof that this bead's changesets walk clean
 * from empty.
 *
 * <p>{@link #changesetsWalkOverAPopulatedStore_preExistingRowSurvives_reRunIsNoOp()}
 * uses a genuinely DEDICATED container ({@link PgContainerHelper#startDedicated()})
 * to prove the three properties a shared-cluster read cannot: (1) the walk
 * succeeds when a real, pre-existing {@code catalog_collections} row is
 * already present ahead of this bead's changesets (the populated-store case
 * the bead's acceptance criteria name explicitly), (2) that pre-existing row
 * survives untouched with the two new columns left NULL, and (3) re-applying
 * the full changelog against this SAME already-migrated database is a no-op
 * -- Liquibase's own checksum verification would throw on any drift, and
 * {@code embedding_models} would hold 8 rows instead of 4 if the seed
 * changeset (catalog-036-2) had somehow re-run.
 */
class Catalog036EmbeddingProfileSchemaLiquibaseTest {

    private static final String MASTER_CHANGELOG = "db/changelog/db.changelog-master.xml";
    private static final String TARGET_CHANGESET_ID = "catalog-036-1";

    // ── Tests 1-4: shape + seed content on the shared, already-migrated cluster ──

    @Test
    void embeddingModels_hasExactlyFourSeededRowsAndNoVoyage3() throws Exception {
        try (var pg = PgContainerHelper.start();
             Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);

            assertThat(PgCatalogProbes.tableExists(ctx, "nexus", "embedding_models"))
                .as("nexus.embedding_models must exist after Liquibase").isTrue();

            var rows = ctx.select(EMBEDDING_MODELS.EMBEDDING_MODEL, EMBEDDING_MODELS.DIMENSION,
                                   EMBEDDING_MODELS.PROVIDER)
                .from(EMBEDDING_MODELS)
                .fetch();

            assertThat(rows).as("embedding_models must hold exactly the four seeded rows").hasSize(4);

            Map<String, Integer> dimensionByModel = rows.stream().collect(Collectors.toMap(
                r -> r.get(EMBEDDING_MODELS.EMBEDDING_MODEL), r -> r.get(EMBEDDING_MODELS.DIMENSION)));
            assertThat(dimensionByModel)
                .as("the four seeded (model -> dimension) pairs must match both authorities "
                    + "byte-for-byte (PgVectorRepository.MODEL_DIMS / corpus.py's "
                    + "CANONICAL_EMBEDDING_MODELS + LOCAL_EMBEDDING_MODELS)")
                .isEqualTo(Map.of(
                    "voyage-code-3", 1024,
                    "voyage-context-3", 1024,
                    "bge-base-en-v15-768", 768,
                    "minilm-l6-v2-384", 384));
            assertThat(dimensionByModel)
                .as("voyage-3 is on the engine's MODEL_DIMS but has no client token and no live "
                    + "row (RDR-204 Key Discoveries) -- must NOT be seeded")
                .doesNotContainKey("voyage-3");

            Map<String, String> providerByModel = rows.stream().collect(Collectors.toMap(
                r -> r.get(EMBEDDING_MODELS.EMBEDDING_MODEL), r -> r.get(EMBEDDING_MODELS.PROVIDER)));
            assertThat(providerByModel)
                .as("provider values reuse EmbedderRouter.modeName()'s own two mode tokens")
                .isEqualTo(Map.of(
                    "voyage-code-3", "voyage",
                    "voyage-context-3", "voyage",
                    "bge-base-en-v15-768", "onnx-local",
                    "minilm-l6-v2-384", "onnx-local"));
        }
    }

    @Test
    void embeddingModels_isInstallScopedWithNoTenantColumnAndNoRls() throws Exception {
        try (var pg = PgContainerHelper.start();
             Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);

            assertThat(PgCatalogProbes.columnExists(ctx, "nexus", "embedding_models", "tenant_id"))
                .as("embedding_models is an install-scoped reference table, no tenant_id column "
                    + "-- same posture as service_tokens/install_pings")
                .isFalse();

            PgCatalogProbes.RowSecurity rls = PgCatalogProbes.rowSecurity(ctx, "nexus", "embedding_models");
            assertThat(rls).as("embedding_models must exist in pg_class").isNotNull();
            assertThat(rls.enabled()).as("embedding_models: no RLS ENABLE (no tenant to scope by)").isFalse();
            assertThat(rls.forced()).as("embedding_models: no RLS FORCE").isFalse();
        }
    }

    @Test
    void embeddingProfile_isTenantScopedWithRlsPolicyAndModelForeignKey() throws Exception {
        try (var pg = PgContainerHelper.start();
             Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);

            assertThat(PgCatalogProbes.tableExists(ctx, "nexus", "embedding_profile"))
                .as("nexus.embedding_profile must exist after Liquibase").isTrue();

            PgCatalogProbes.RowSecurity rls = PgCatalogProbes.rowSecurity(ctx, "nexus", "embedding_profile");
            assertThat(rls).as("embedding_profile must exist in pg_class").isNotNull();
            assertThat(rls.enabled()).as("embedding_profile RLS ENABLE (tenant-scoped table)").isTrue();
            assertThat(rls.forced()).as("embedding_profile RLS FORCE (tenant-scoped table)").isTrue();

            assertThat(PgCatalogProbes.policies(ctx, "nexus", "embedding_profile"))
                .as("embedding_profile must have at least one RLS policy (tenant_isolation)")
                .isNotEmpty();

            assertThat(PgCatalogProbes.foreignKey(ctx, "nexus", "embedding_profile_model_fk"))
                .as("embedding_profile.embedding_model must FK to embedding_models.embedding_model")
                .isNotNull();
        }
    }

    @Test
    void catalogCollections_gainsNullableDimensionAndLifecycleStateColumns() throws Exception {
        try (var pg = PgContainerHelper.start();
             Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);

            PgCatalogProbes.ColumnInfo dimension =
                PgCatalogProbes.columnInfo(ctx, "nexus", "catalog_collections", "dimension");
            assertThat(dimension).as("catalog_collections.dimension must exist").isNotNull();
            assertThat(dimension.nullable())
                .as("dimension must be NULLABLE at this step -- the backfill walk (bead "
                    + "nexus-ft04v.4) populates it and adds constraints in the same changeset")
                .isTrue();

            PgCatalogProbes.ColumnInfo lifecycleState =
                PgCatalogProbes.columnInfo(ctx, "nexus", "catalog_collections", "lifecycle_state");
            assertThat(lifecycleState).as("catalog_collections.lifecycle_state must exist").isNotNull();
            assertThat(lifecycleState.nullable())
                .as("lifecycle_state must be NULLABLE at this step, no CHECK constraint yet")
                .isTrue();
        }
    }

    // ── Test 5: populated store, walk-from-empty, and re-run no-op (dedicated container) ──

    @Test
    void changesetsWalkOverAPopulatedStore_preExistingRowSurvives_reRunIsNoOp() throws Exception {
        PostgreSQLContainer<?> pg = PgContainerHelper.startDedicated();
        try {
            // Phase 1: migrate up to (NOT including) catalog-036-1 on a
            // GENUINELY fresh, dedicated database -- this IS the walk-from-
            // empty proof for every changeset that precedes this bead's own,
            // and sets up the pre-existing row below to predate this bead's
            // changesets entirely (Hygiene001NotNullMigrationRlsTest's idiom).
            try (Connection su = pg.createConnection("")) {
                migrateUpTo(su, TARGET_CHANGESET_ID);
            }

            String tenant = "catalog036-pop-tenant";
            String collection = tenant + "__legacy-coll__voyage-context-3__v1";
            try (Connection su = pg.createConnection("")) {
                su.setAutoCommit(true);
                DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
                ctx.insertInto(CATALOG_COLLECTIONS, CATALOG_COLLECTIONS.TENANT_ID, CATALOG_COLLECTIONS.NAME)
                    .values(tenant, collection)
                    .execute();
            }

            // Phase 2: apply the rest of the changelog (catalog-036-1 through
            // -4 and everything after) over that populated store -- proves
            // the walk never fails with real, pre-existing data present.
            try (Connection su = pg.createConnection("")) {
                applyFullChangelog(su);
            }

            // Phase 3: the pre-existing row survives untouched, with the two
            // new columns left NULL -- this bead's changesets are pure
            // ADD COLUMN, no backfill DML.
            try (Connection su = pg.createConnection("")) {
                DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
                var row = ctx.select(CATALOG_COLLECTIONS.DIMENSION, CATALOG_COLLECTIONS.LIFECYCLE_STATE)
                    .from(CATALOG_COLLECTIONS)
                    .where(CATALOG_COLLECTIONS.TENANT_ID.eq(tenant))
                    .and(CATALOG_COLLECTIONS.NAME.eq(collection))
                    .fetchOne();
                assertThat(row).as("the pre-existing collection row must survive the walk").isNotNull();
                assertThat(row.get(CATALOG_COLLECTIONS.DIMENSION))
                    .as("dimension must be NULL for a pre-existing row -- the backfill walk "
                        + "(bead nexus-ft04v.4), not this changeset, fills it")
                    .isNull();
                assertThat(row.get(CATALOG_COLLECTIONS.LIFECYCLE_STATE))
                    .as("lifecycle_state must be NULL for a pre-existing row")
                    .isNull();
            }

            // Phase 4: re-apply the FULL changelog on this SAME,
            // already-migrated database. This bead's four changesets are
            // non-runAlways, so Liquibase must execute ZERO of them again --
            // a checksum mismatch on any one of them would throw here.
            try (Connection su = pg.createConnection("")) {
                applyFullChangelog(su);
            }

            // A re-run that (incorrectly) re-executed catalog-036-2 would
            // leave embedding_models holding 8 rows instead of 4.
            try (Connection su = pg.createConnection("")) {
                DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
                assertThat(ctx.selectCount().from(EMBEDDING_MODELS).fetchOne(0, int.class))
                    .as("re-applying the changelog must not re-seed embedding_models")
                    .isEqualTo(4);
            }
        } finally {
            pg.stop();
        }
    }

    // ── Helpers ───────────────────────────────────────────────────────────

    private static void applyFullChangelog(Connection conn) throws Exception {
        // Deliberately NOT try-with-resources on the Liquibase object (matches
        // PgContainerHelper#applyProductSchema's own idiom, see its javadoc):
        // Liquibase#close() closes the underlying JdbcConnection it wraps, so
        // closing it here would leave the setAutoCommit(true) restoration
        // below (and every subsequent use of `conn` by the caller) hitting an
        // already-closed connection.
        Database database = DatabaseFactory.getInstance()
            .findCorrectDatabaseImplementation(new JdbcConnection(conn));
        Liquibase liquibase = new Liquibase(
            MASTER_CHANGELOG, new ClassLoaderResourceAccessor(), database);
        liquibase.update(new Contexts(), new LabelExpression());
        conn.setAutoCommit(true);
    }

    /**
     * Apply the master changelog's changesets UP TO, but NOT INCLUDING,
     * {@code targetChangesetId} -- {@code Hygiene001NotNullMigrationRlsTest}'s
     * identical index-based idiom (robust against other changesets landing
     * earlier in the chain). See {@link #applyFullChangelog} for why the
     * {@link Liquibase} instance here is not try-with-resources'd either.
     */
    private static void migrateUpTo(Connection conn, String targetChangesetId) throws Exception {
        Database database = DatabaseFactory.getInstance()
            .findCorrectDatabaseImplementation(new JdbcConnection(conn));
        Liquibase liquibase = new Liquibase(
            MASTER_CHANGELOG, new ClassLoaderResourceAccessor(), database);
        List<ChangeSet> unrun = liquibase.listUnrunChangeSets(new Contexts(), new LabelExpression());
        int idx = -1;
        for (int i = 0; i < unrun.size(); i++) {
            if (targetChangesetId.equals(unrun.get(i).getId())) {
                idx = i;
                break;
            }
        }
        assertThat(idx)
            .as(targetChangesetId + " must be present in the master changelog")
            .isGreaterThanOrEqualTo(0);
        liquibase.update(idx, new Contexts(), new LabelExpression());
        conn.setAutoCommit(true);
    }
}
