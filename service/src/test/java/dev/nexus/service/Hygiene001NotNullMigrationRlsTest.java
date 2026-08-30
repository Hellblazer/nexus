// SPDX-License-Identifier: AGPL-3.0-or-later
package dev.nexus.service;

import liquibase.Contexts;
import liquibase.LabelExpression;
import liquibase.Liquibase;
import liquibase.changelog.ChangeSet;
import liquibase.database.Database;
import liquibase.database.DatabaseFactory;
import liquibase.database.jvm.JdbcConnection;
import liquibase.resource.ClassLoaderResourceAccessor;
import org.junit.jupiter.api.Test;
import org.postgresql.util.PSQLException;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.sql.ResultSet;
import java.util.List;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * nexus-tk070.p6a follow-on (T2 nexus/nullable-column-inventory-2026-08-29) —
 * proof that {@code hygiene-001-not-null.xml}'s FORCE-RLS-toggled DML actually
 * runs (rather than silently no-op'ing) when Liquibase migrates as a genuine
 * NOBYPASSRLS schema-owner role, and that the SET NOT NULL DDL that follows
 * each toggle really takes effect.
 *
 * <p>THE TRAP THIS TEST GUARDS AGAINST (2026-07-08 v0.1.33 production
 * incident, nexus-1wjmq): every table hygiene-001 touches carries FORCE ROW
 * LEVEL SECURITY, and Liquibase's production role ({@code nexus_admin}) is
 * the table OWNER but holds NO BYPASSRLS, with no {@code nexus.tenant} GUC
 * set at migration time. An UN-toggled DELETE/UPDATE under that combination
 * silently matches ZERO rows for EVERY tenant — the DDL that follows (SET NOT
 * NULL) then sees the untouched NULL rows and FAILS, while CI stays green
 * because Testcontainers normally runs Liquibase as the Postgres superuser
 * (implicit BYPASSRLS), which cannot reproduce the no-op. This test uses a
 * real NOSUPERUSER/NOBYPASSRLS role for the ENTIRE migration, exactly like
 * {@link SchemaMigratorIntegrationTest} and {@link AspectDocIdBackfillTest}.
 *
 * <p>Uses a DEDICATED container ({@link PgContainerHelper#startDedicated()})
 * and Liquibase's changeset-count-limited {@code update(int, ...)}
 * ({@link #migrateUpTo}, the exact idiom {@code AspectDocIdBackfillTest}
 * already established) to migrate up to — but NOT including —
 * {@code hygiene-001-1}, seed pre-existing legacy rows in TWO TENANTS, THEN
 * apply the remainder of the changelog (hygiene-001-1 through -11 inclusive)
 * and assert: the NULL rows are gone (or backfilled) in BOTH tenants, and
 * the NOT NULL constraint genuinely rejects a fresh NULL insert afterward
 * (proves the DDL took effect, not merely that no NULL rows happen to
 * remain right now).
 *
 * <p>Scoped to {@code document_aspects} (hygiene-001-1): the richest of the
 * eleven steps — it carries BOTH a DELETE (the doc_id-orphan class) and an
 * UPDATE (the source_uri backfill) ahead of a two-column SET NOT NULL, so a
 * single test here exercises every mechanism (DELETE + UPDATE + toggle +
 * DDL) the other ten steps also use, just with different tables/predicates.
 */
class Hygiene001NotNullMigrationRlsTest {

    private static final String TARGET_CHANGESET_ID = "hygiene-001-1";
    private static final String MASTER_CHANGELOG = "db/changelog/db.changelog-master.xml";
    private static final String TENANT_1 = "hygiene001-rls-tenant-1";
    private static final String TENANT_2 = "hygiene001-rls-tenant-2";

    @Test
    void hygieneChangeset_deletesOrphansAndBackfillsAcrossTwoTenants_andNotNullTakesEffect()
            throws Exception {
        PostgreSQLContainer<?> pg = PgContainerHelper.startDedicated();
        try {
            final String role = "nexus_admin_hygiene001_test";
            final String pass = "nexus_admin_hygiene001_test_pass";
            bootstrapAdminRole(pg, role, pass);

            var cfg = new com.zaxxer.hikari.HikariConfig();
            cfg.setJdbcUrl(pg.getJdbcUrl());
            cfg.setUsername(role);
            cfg.setPassword(pass);
            cfg.setMaximumPoolSize(2);
            cfg.setPoolName("nexus-admin-hygiene001-test");

            try (var adminDs = new com.zaxxer.hikari.HikariDataSource(cfg)) {
                // Phase 1: migrate up to (NOT including) hygiene-001-1, so the
                // legacy rows below can be seeded BEFORE the hygiene changesets
                // ever run -- exactly AspectDocIdBackfillTest's idiom.
                migrateUpTo(adminDs, TARGET_CHANGESET_ID);

                // Phase 2: seed legacy state in TWO TENANTS. Per tenant:
                //   (a) an ORPHAN row: doc_id IS NULL -- must be DELETED.
                //   (b) a LEGIT row: doc_id already set (real catalog document),
                //       source_uri IS NULL -- must be BACKFILLED to '' and
                //       survive, doc_id untouched.
                for (String tenant : List.of(TENANT_1, TENANT_2)) {
                    seedLegacyState(pg, tenant);
                }

                // Phase 2b: one genuinely legacy-width (16-byte, un-rekeyed)
                // chunks row + its catalog_document_chunks manifest row, per
                // tenant -- hygiene-001-5/-8's restrict-then-delete coverage
                // (nexus-tk070.p6a follow-on). By this point in the changelog
                // (migrateUpTo already ran vectors-004-unify-chunks.xml and
                // rdr180-001-bytea-chash.xml), chunks_chash_octet_check and
                // catalog_document_chunks_chash_octet_check are LIVE NOT VALID
                // constraints -- NOT VALID exempts only pre-existing rows at
                // ADD time, every INSERT/UPDATE from then on IS checked, so a
                // plain INSERT of a 16-byte chash here would be rejected
                // outright. The two constraints are dropped, the legacy rows
                // seeded, then the constraints re-added NOT VALID -- restoring
                // the exact aged-fleet shape rdr180-001 itself documents
                // (present, unenforced against pre-existing rows) before
                // Phase 3 runs hygiene-001-5/-8 against it.
                try (Connection su = pg.createConnection("")) {
                    su.setAutoCommit(true);
                    su.createStatement().execute(
                        "ALTER TABLE nexus.chunks DROP CONSTRAINT chunks_chash_octet_check");
                    su.createStatement().execute(
                        "ALTER TABLE nexus.catalog_document_chunks "
                        + "DROP CONSTRAINT catalog_document_chunks_chash_octet_check");

                    seedLegacyChunkAndManifest(su, TENANT_1, "legacywidthchsh1");
                    seedLegacyChunkAndManifest(su, TENANT_2, "legacywidthchsh2");

                    su.createStatement().execute(
                        "ALTER TABLE nexus.chunks "
                        + "ADD CONSTRAINT chunks_chash_octet_check "
                        + "CHECK (octet_length(chash) = 32) NOT VALID");
                    su.createStatement().execute(
                        "ALTER TABLE nexus.catalog_document_chunks "
                        + "ADD CONSTRAINT catalog_document_chunks_chash_octet_check "
                        + "CHECK (octet_length(chash) = 32) NOT VALID");
                }

                // Phase 2c (coordinator item E, review round): catalog_collections
                // .created_at VALUE correctness, not merely non-null-ness -- one
                // collection whose two documents carry known, distinct indexed_at
                // (created_at must backfill to the MIN, not now()), and one
                // collection with zero documents (created_at must still fall back
                // to now(), never left NULL).
                for (String tenant : List.of(TENANT_1, TENANT_2)) {
                    seedCollectionCreatedAtScenario(pg, tenant);
                }

                // Phase 3: apply the rest of the changelog (hygiene-001-1
                // through hygiene-001-11 inclusive, plus grants).
                try (Connection conn = adminDs.getConnection()) {
                    Database database = DatabaseFactory.getInstance()
                        .findCorrectDatabaseImplementation(new JdbcConnection(conn));
                    try (Liquibase liquibase = new Liquibase(
                            MASTER_CHANGELOG, new ClassLoaderResourceAccessor(), database)) {
                        liquibase.update(new Contexts(), new LabelExpression());
                    }
                }

                // Phase 4: assert per-tenant outcomes.
                try (Connection su = pg.createConnection("")) {
                    for (String tenant : List.of(TENANT_1, TENANT_2)) {
                        ResultSet orphan = su.createStatement().executeQuery(
                            "SELECT 1 FROM nexus.document_aspects "
                            + "WHERE tenant_id = '" + tenant + "' AND source_path = 'orphan.md'");
                        assertThat(orphan.next())
                            .as("hygiene-001-1's DELETE WHERE doc_id IS NULL must have removed "
                                + "the orphan row for tenant " + tenant + " -- a false result here "
                                + "means the toggle-wrapped DELETE silently no-op'd under FORCE RLS "
                                + "(the exact nexus-1wjmq production incident)")
                            .isFalse();

                        ResultSet legit = su.createStatement().executeQuery(
                            "SELECT doc_id, source_uri FROM nexus.document_aspects "
                            + "WHERE tenant_id = '" + tenant + "' AND source_path = 'legit.md'");
                        assertThat(legit.next())
                            .as("the legit row for tenant " + tenant + " must survive the DELETE")
                            .isTrue();
                        assertThat(legit.getString("doc_id"))
                            .as("the legit row's pre-existing doc_id must be untouched")
                            .isEqualTo(tenant + "-doc-1");
                        assertThat(legit.getString("source_uri"))
                            .as("hygiene-001-1's UPDATE must have backfilled the legit row's "
                                + "NULL source_uri to '' for tenant " + tenant + " -- a null/absent "
                                + "result here means that toggle-wrapped UPDATE also no-op'd")
                            .isEqualTo("");

                        // hygiene-001-5/-8: the legacy-width chunk and its
                        // manifest row must both be GONE -- deleted as the
                        // un-rekeyed, unresolvable-since-the-32-byte-cutover
                        // class (nexus-lgdel), never left behind and never
                        // aborting the walk.
                        String legacyChash = "legacywidthchsh" + (tenant.equals(TENANT_1) ? "1" : "2");
                        ResultSet chunkGone = su.createStatement().executeQuery(
                            "SELECT 1 FROM nexus.chunks WHERE tenant_id = '" + tenant
                            + "' AND chash = '" + legacyChash + "'");
                        assertThat(chunkGone.next())
                            .as("hygiene-001-5's DELETE WHERE octet_length(chash) <> 32 must have "
                                + "removed the legacy-width chunks row for tenant " + tenant)
                            .isFalse();

                        ResultSet manifestGone = su.createStatement().executeQuery(
                            "SELECT 1 FROM nexus.catalog_document_chunks WHERE tenant_id = '" + tenant
                            + "' AND chash = '" + legacyChash + "'");
                        assertThat(manifestGone.next())
                            .as("the legacy-width chunk's catalog_document_chunks manifest row must "
                                + "also be gone for tenant " + tenant + " -- either via hygiene-001-5's "
                                + "own FK-safety pre-delete (ahead of the chunks DELETE) or "
                                + "hygiene-001-8's independent chunk_index cleanup")
                            .isFalse();

                        // hygiene-001-6 (coordinator item E, review round): VALUE
                        // correctness, not merely non-null-ness. Collection A's
                        // created_at must equal the MIN indexed_at of its two
                        // documents (2026-01-01, the earlier of the two seeded),
                        // never the later document's indexed_at and never now().
                        String collA = tenant + "__created-at-two-docs__v1";
                        ResultSet collAIsMin = su.createStatement().executeQuery(
                            "SELECT created_at = '2026-01-01T00:00:00+00:00'::timestamptz AS is_min "
                            + "FROM nexus.catalog_collections "
                            + "WHERE tenant_id = '" + tenant + "' AND name = '" + collA + "'");
                        assertThat(collAIsMin.next())
                            .as("collection A must exist for tenant " + tenant)
                            .isTrue();
                        assertThat(collAIsMin.getBoolean("is_min"))
                            .as("collection A's created_at must equal the MIN indexed_at "
                                + "(2026-01-01) of its two documents for tenant " + tenant
                                + " -- not the later document's indexed_at (2026-06-01) and "
                                + "not now()")
                            .isTrue();

                        // Collection B has zero documents: the correlated subquery
                        // returns no row, so COALESCE must fall back to now() --
                        // created_at must still end up NOT NULL, never left NULL.
                        String collB = tenant + "__created-at-no-docs__v1";
                        ResultSet collBCreatedAt = su.createStatement().executeQuery(
                            "SELECT created_at FROM nexus.catalog_collections "
                            + "WHERE tenant_id = '" + tenant + "' AND name = '" + collB + "'");
                        assertThat(collBCreatedAt.next())
                            .as("collection B must exist for tenant " + tenant)
                            .isTrue();
                        assertThat(collBCreatedAt.getTimestamp("created_at"))
                            .as("collection B has no documents for tenant " + tenant
                                + " -- created_at must fall back to now(), never left NULL")
                            .isNotNull();
                    }

                    // NOT NULL genuinely took effect -- not merely "no NULL rows remain
                    // right now". A raw INSERT with doc_id IS NULL must be rejected.
                    assertThatThrownBy(() -> su.createStatement().execute(
                        "INSERT INTO nexus.document_aspects "
                        + "  (tenant_id, collection, source_path, extracted_at, model_version, "
                        + "   extractor_name, source_uri, doc_id) "
                        + "VALUES ('" + TENANT_1 + "', 'post-migration-coll', 'post-migration.md', "
                        + "now(), 'v1', 'ex', 'file:///post.md', NULL)"))
                        .as("SET NOT NULL on doc_id must genuinely reject a fresh NULL insert "
                            + "post-migration")
                        .isInstanceOf(PSQLException.class)
                        .hasMessageContaining("null value in column \"doc_id\"");

                    assertThatThrownBy(() -> su.createStatement().execute(
                        "INSERT INTO nexus.document_aspects "
                        + "  (tenant_id, collection, source_path, extracted_at, model_version, "
                        + "   extractor_name, source_uri, doc_id) "
                        + "VALUES ('" + TENANT_1 + "', 'post-migration-coll', 'post-migration-2.md', "
                        + "now(), 'v1', 'ex', NULL, '" + TENANT_1 + "-doc-1')"))
                        .as("SET NOT NULL on source_uri must genuinely reject a fresh NULL insert "
                            + "post-migration")
                        .isInstanceOf(PSQLException.class)
                        .hasMessageContaining("null value in column \"source_uri\"");
                }
            }
        } finally {
            pg.stop();
        }
    }

    /**
     * Seeds a catalog_collections row, TWO catalog_documents rows (one live,
     * used as the legit aspect row's real attribution target), and two
     * document_aspects rows (orphan.md: doc_id IS NULL; legit.md: doc_id set,
     * source_uri IS NULL) for one tenant.
     */
    private static void seedLegacyState(PostgreSQLContainer<?> pg, String tenant) throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            String collection = tenant + "__coll__voyage-context-3__v1";
            su.createStatement().execute(
                "INSERT INTO nexus.catalog_collections (tenant_id, name) "
                + "VALUES ('" + tenant + "', '" + collection + "')");
            su.createStatement().execute(
                "INSERT INTO nexus.catalog_documents (tenant_id, tumbler, title, physical_collection) "
                + "VALUES ('" + tenant + "', '" + tenant + "-doc-1', 'Doc', '" + collection + "')");

            // Orphan: doc_id IS NULL, no catalog document attributes it (the
            // 554-row chroma:// class the changeset's header describes) --
            // must be DELETED.
            su.createStatement().execute(
                "INSERT INTO nexus.document_aspects "
                + "  (tenant_id, collection, source_path, extracted_at, model_version, "
                + "   extractor_name, source_uri, doc_id) "
                + "VALUES ('" + tenant + "', '" + collection + "', 'orphan.md', "
                + "'2025-01-01T00:00:00+00'::timestamptz, 'legacy-model', 'legacy-extractor', "
                + "'chroma://" + collection + "/orphan.md', NULL)");

            // Legit: doc_id already attributes to a real catalog document,
            // source_uri IS NULL (the straggler class) -- must SURVIVE with
            // doc_id unchanged and source_uri backfilled to ''.
            su.createStatement().execute(
                "INSERT INTO nexus.document_aspects "
                + "  (tenant_id, collection, source_path, extracted_at, model_version, "
                + "   extractor_name, source_uri, doc_id) "
                + "VALUES ('" + tenant + "', '" + collection + "', 'legit.md', "
                + "'2025-01-01T00:00:00+00'::timestamptz, 'legacy-model', 'legacy-extractor', "
                + "NULL, '" + tenant + "-doc-1')");
        }
    }

    /**
     * A genuinely legacy-width (16-byte, un-rekeyed) chunks row plus its
     * catalog_document_chunks manifest row referencing the SAME chash, for
     * one tenant -- hygiene-001-5/-8 restrict-then-delete coverage
     * (nexus-tk070.p6a follow-on). {@code legacyChash} must be EXACTLY 16
     * characters: Postgres's bytea escape-format literal input takes each
     * printable-ASCII character as one raw byte, so a 16-char literal here
     * becomes a 16-byte bytea value (octet_length = 16, failing the
     * octet_length(chash) = 32 predicate both restrict-then-delete
     * changesets use) -- the same implicit-cast idiom
     * CatalogDocumentCascadeTest#chash already relies on for its (32-char,
     * well-formed) chash literals.
     *
     * <p>Reuses tenant + "-doc-1" (registered by {@link #seedLegacyState})
     * as the manifest row's doc_id, and the same collection name, so no
     * separate document/collection seeding is needed and the
     * fk_catalog_chunks_chunk FK ({@code catalog-029-manifest-chunk-fk.xml})
     * is satisfied by construction. metadata and chunk_index are both left
     * unset (implicit NULL) -- the exact shape hygiene-001-5/-8 target.
     *
     * <p>Caller must have already dropped chunks_chash_octet_check and
     * catalog_document_chunks_chash_octet_check (both live, NOT VALID, and
     * enforced against NEW rows going forward since rdr180-001 /
     * vectors-004-unify-chunks.xml already ran by migrateUpTo's target) —
     * otherwise this INSERT is rejected outright.
     */
    private static void seedLegacyChunkAndManifest(Connection su, String tenant, String legacyChash)
            throws Exception {
        String collection = tenant + "__coll__voyage-context-3__v1";
        su.createStatement().execute(
            "INSERT INTO nexus.chunks (tenant_id, collection, chash, chunk_text, embedding_384) "
            + "VALUES ('" + tenant + "', '" + collection + "', '" + legacyChash + "', "
            + "'legacy un-rekeyed chunk text', ('[" + "0,".repeat(383) + "0]')::vector)");
        su.createStatement().execute(
            "INSERT INTO nexus.catalog_document_chunks (tenant_id, doc_id, position, chash, collection) "
            + "VALUES ('" + tenant + "', '" + tenant + "-doc-1', 0, '" + legacyChash + "', '"
            + collection + "')");
    }

    /**
     * Coordinator item E (review round): a catalog_collections VALUE-correctness
     * fixture for hygiene-001-6, for one tenant. Two collections:
     *
     * <ul>
     *   <li>{@code <tenant>__created-at-two-docs__v1} — two catalog_documents
     *       rows with KNOWN, DISTINCT {@code indexed_at} values
     *       (2026-01-01, 2026-06-01). hygiene-001-6's backfill UPDATE must set
     *       {@code created_at} to the MIN of the two (2026-01-01), proving the
     *       correlated subquery's aggregate -- not merely that SOME non-null
     *       value landed.</li>
     *   <li>{@code <tenant>__created-at-no-docs__v1} — zero documents. The
     *       correlated subquery returns no row, so COALESCE must fall back to
     *       {@code now()} -- proving the fallback arm, not just the MIN arm.</li>
     * </ul>
     *
     * {@code indexed_at} is TEXT (catalog-001-baseline.xml), so the two seeded
     * values are ISO-8601 strings that also sort correctly LEXICOGRAPHICALLY as
     * chronological order (same format/width, differing only in the month
     * digits) -- matching what {@code min(d.indexed_at)} actually computes.
     */
    private static void seedCollectionCreatedAtScenario(PostgreSQLContainer<?> pg, String tenant)
            throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);

            String collA = tenant + "__created-at-two-docs__v1";
            su.createStatement().execute(
                "INSERT INTO nexus.catalog_collections (tenant_id, name) "
                + "VALUES ('" + tenant + "', '" + collA + "')");
            su.createStatement().execute(
                "INSERT INTO nexus.catalog_documents "
                + "  (tenant_id, tumbler, title, physical_collection, indexed_at) "
                + "VALUES ('" + tenant + "', '" + tenant + "-created-at-doc-1', 'Doc1', '"
                + collA + "', '2026-01-01T00:00:00+00:00')");
            su.createStatement().execute(
                "INSERT INTO nexus.catalog_documents "
                + "  (tenant_id, tumbler, title, physical_collection, indexed_at) "
                + "VALUES ('" + tenant + "', '" + tenant + "-created-at-doc-2', 'Doc2', '"
                + collA + "', '2026-06-01T00:00:00+00:00')");

            String collB = tenant + "__created-at-no-docs__v1";
            su.createStatement().execute(
                "INSERT INTO nexus.catalog_collections (tenant_id, name) "
                + "VALUES ('" + tenant + "', '" + collB + "')");
        }
    }

    /** Minimal DBA-equivalent bootstrap, mirroring AspectDocIdBackfillTest's identical helper. */
    private static void bootstrapAdminRole(PostgreSQLContainer<?> pg, String role, String pass)
            throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            su.createStatement().execute(
                "CREATE ROLE " + role + " LOGIN PASSWORD '" + pass
                    + "' NOSUPERUSER NOCREATEDB NOCREATEROLE");
            su.createStatement().execute("GRANT CREATE ON DATABASE postgres TO " + role);
            su.createStatement().execute("GRANT CREATE ON SCHEMA public TO " + role);
            su.createStatement().execute("GRANT pg_monitor TO " + role + " WITH ADMIN OPTION");
            su.createStatement().execute(
                "CREATE ROLE nexus_svc LOGIN PASSWORD 'nexus_svc_pass' "
                + "NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS");
            su.createStatement().execute("CREATE EXTENSION IF NOT EXISTS vector");
            su.createStatement().execute("CREATE EXTENSION IF NOT EXISTS pg_trgm");
        }
    }

    /**
     * Apply the master changelog's changesets UP TO, but NOT INCLUDING,
     * {@code targetChangesetId} -- AspectDocIdBackfillTest's identical
     * index-based idiom (robust against other changesets landing earlier
     * in the chain).
     */
    private static void migrateUpTo(com.zaxxer.hikari.HikariDataSource adminDs,
                                    String targetChangesetId) throws Exception {
        try (Connection conn = adminDs.getConnection()) {
            Database database = DatabaseFactory.getInstance()
                .findCorrectDatabaseImplementation(new JdbcConnection(conn));
            try (Liquibase liquibase = new Liquibase(
                    MASTER_CHANGELOG, new ClassLoaderResourceAccessor(), database)) {
                List<ChangeSet> unrun = liquibase.listUnrunChangeSets(
                    new Contexts(), new LabelExpression());
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
            }
        }
    }
}
