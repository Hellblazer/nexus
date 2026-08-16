// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.db.CatalogRepository;
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

import java.sql.Connection;
import java.sql.ResultSet;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;
import static org.junit.jupiter.api.Assertions.assertThrows;

/**
 * RDR-194 Phase P1 (Decision D2, bead nexus-tk070.p1) — {@code
 * fk_catalog_links_from_document} / {@code fk_catalog_links_to_document}: the
 * two FKs from {@code nexus.catalog_links (tenant_id, from_tumbler|to_tumbler)}
 * to {@code nexus.catalog_documents (tenant_id, tumbler)}.
 *
 * <p>Modeled on {@link ManifestChunkFkTest}'s structure (constraint shape,
 * accept/reject boundary, then delete-cascade behaviour), but this FK is
 * PLAIN — non-deferrable, ON DELETE CASCADE ON UPDATE CASCADE (RDR-194 § D2
 * verbatim) — so there is no class-B deferred-constraint group here; the two
 * production hard-delete call sites (Java {@code deleteCollectionTxn}, plpgsql
 * {@code purge_trash} Step 4) are covered by {@link CatalogDocumentCascadeTest}
 * and {@link CatalogPurgeTrashTest} respectively, which is the entire point of
 * D2's decision to use the FK instead of an explicit Java step: no ordering
 * dance is needed for either call site.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
@TestMethodOrder(MethodOrderer.OrderAnnotation.class)
class CatalogLinksTumblerFkTest {

    private static final String FK_FROM = "fk_catalog_links_from_document";
    private static final String FK_TO = "fk_catalog_links_to_document";
    private static final String SVC_ROLE = "svc_links_fk_test";
    private static final String SVC_PASS = "svc_links_fk_test_pass";
    private static final String TENANT = "links-fk-tenant";
    private static final String COLLECTION = "knowledge__links-fk__minilm-l6-v2-384__v1";

    PostgreSQLContainer<?> pg;
    HikariDataSource svcDs;
    TenantScope tenantScope;
    CatalogRepository catalogRepo;

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();

        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            su.createStatement().execute(
                "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '" + SVC_ROLE + "') THEN "
                + "CREATE ROLE " + SVC_ROLE + " LOGIN PASSWORD '" + SVC_PASS + "' NOSUPERUSER NOBYPASSRLS; END IF; END $$");
            su.createStatement().execute(
                "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nexus_svc') THEN "
                + "CREATE ROLE nexus_svc LOGIN PASSWORD 'nexus_svc_pass' NOSUPERUSER NOBYPASSRLS; END IF; END $$");
        }

        try (Connection su = pg.createConnection("")) {
            var lb = new Liquibase("db/changelog/db.changelog-master.xml",
                new ClassLoaderResourceAccessor(),
                DatabaseFactory.getInstance().findCorrectDatabaseImplementation(new JdbcConnection(su)));
            lb.update(new Contexts());
        }

        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            su.createStatement().execute("GRANT USAGE ON SCHEMA nexus TO " + SVC_ROLE);
            su.createStatement().execute(
                "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA nexus TO " + SVC_ROLE);
            su.createStatement().execute(
                "GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA nexus TO " + SVC_ROLE);
            su.createStatement().execute("ALTER ROLE " + SVC_ROLE + " SET search_path TO nexus, public");
        }

        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(SVC_ROLE);
        cfg.setPassword(SVC_PASS);
        cfg.setMaximumPoolSize(5);
        cfg.setAutoCommit(true);
        svcDs = new HikariDataSource(cfg);
        tenantScope = new TenantScope(svcDs);
        catalogRepo = new CatalogRepository(tenantScope);
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    // ══════════════════════════════════════════════════════════════════════════
    // GROUP 1 — constraint shape (D2 verbatim)
    // ══════════════════════════════════════════════════════════════════════════

    @Test
    @Order(10)
    void bothFks_existValidatedNotDeferrable_onDeleteCascade_onUpdateCascade() throws Exception {
        try (Connection su = pg.createConnection("")) {
            for (String fkName : new String[]{FK_FROM, FK_TO}) {
                ResultSet rs = su.createStatement().executeQuery(
                    "SELECT convalidated, condeferrable, confupdtype, confdeltype "
                    + "FROM pg_constraint c JOIN pg_namespace n ON n.oid = c.connamespace "
                    + "WHERE c.contype = 'f' AND c.conname = '" + fkName + "' AND n.nspname = 'nexus'");
                assertThat(rs.next()).as(fkName + " must exist in pg_constraint").isTrue();
                assertThat(rs.getBoolean("convalidated"))
                    .as(fkName + " must be VALIDATED (catalog-032-2/3 ran)").isTrue();
                assertThat(rs.getBoolean("condeferrable"))
                    .as(fkName + " must be a PLAIN (non-deferrable) FK — unlike fk_catalog_chunks_chunk, "
                        + "no call site needs to defer it (RDR-194 § D2)").isFalse();
                assertThat(rs.getString("confupdtype"))
                    .as(fkName + " ON UPDATE must be CASCADE ('c') — RDR-194 § D2 verbatim").isEqualTo("c");
                assertThat(rs.getString("confdeltype"))
                    .as(fkName + " ON DELETE must be CASCADE ('c') — RDR-194 § D2 verbatim").isEqualTo("c");
            }
        }
    }

    // ══════════════════════════════════════════════════════════════════════════
    // GROUP 2 — accept / reject boundary (CatalogRepository.upsertLink)
    // ══════════════════════════════════════════════════════════════════════════

    @Test
    @Order(20)
    void upsertLink_liveEndpoints_succeeds() {
        seedDoc("fk20-src");
        seedDoc("fk20-dst");
        assertThat(catalogRepo.upsertLink(TENANT, Map.of(
            "from_tumbler", "fk20-src", "to_tumbler", "fk20-dst",
            "link_type", "cites", "created_by", "test"))).isTrue();
    }

    @Test
    @Order(21)
    void upsertLink_nonexistentTumbler_isRejectedByRequireLiveEndpoints_beforeTouchingTheFk() {
        // The default (allow_dangling unset) path never reaches the database at
        // all for this case — requireLiveEndpoints refuses it in Java first.
        // The REAL constraint is provoked separately below (Order 22), only
        // reachable once that Java-level guard is bypassed via allow_dangling.
        seedDoc("fk21-src");
        var ex = assertThrows(
            CatalogRepository.DanglingEndpointException.class, () ->
            catalogRepo.upsertLink(TENANT, Map.of(
                "from_tumbler", "fk21-src", "to_tumbler", "fk21-nonexistent",
                "link_type", "cites", "created_by", "test")));
        assertThat(ex.missing()).containsExactly("to_tumbler");
    }

    @Test
    @Order(22)
    void upsertLink_allowDangling_nonexistentTumbler_provokesTheRealFkViolation() {
        // nexus-tk070.p1 (RDR-194 § D2): allow_dangling=true bypasses
        // requireLiveEndpoints, so this write reaches Postgres and provokes the
        // REAL fk_catalog_links_to_document SQLSTATE 23503 — not a mocked
        // SQLState — which upsertLink's catch (SqlConstraints.violatesAnyFk)
        // maps back to the SAME DanglingEndpointException type.
        seedDoc("fk22-src");
        var ex = assertThrows(
            CatalogRepository.DanglingEndpointException.class, () ->
            catalogRepo.upsertLink(TENANT, Map.of(
                "from_tumbler", "fk22-src", "to_tumbler", "fk22-nonexistent",
                "link_type", "cites", "created_by", "test",
                "allow_dangling", true)));
        assertThat(ex.missing()).containsExactly("to_tumbler");
        assertThat(catalogRepo.linksFrom(TENANT, "fk22-src", (java.util.List<String>) null)).isEmpty();
    }

    @Test
    @Order(23)
    void upsertLink_allowDangling_fromSideNonexistent_namesFromTumbler() {
        // Same as Order 22, mirrored onto fk_catalog_links_from_document, so
        // BOTH constraints are provoked directly (not just one standing in for
        // the other by symmetry assumption).
        seedDoc("fk23-dst");
        var ex = assertThrows(
            CatalogRepository.DanglingEndpointException.class, () ->
            catalogRepo.upsertLink(TENANT, Map.of(
                "from_tumbler", "fk23-nonexistent", "to_tumbler", "fk23-dst",
                "link_type", "cites", "created_by", "test",
                "allow_dangling", true)));
        assertThat(ex.missing()).containsExactly("from_tumbler");
    }

    @Test
    @Order(24)
    void upsertLink_allowDangling_tombstonedEndpoint_stillWrites() {
        // The narrowed (not disappeared) half of allow_dangling's contract
        // (RDR-194 § D2): a row exists (tombstoned), so the FK is satisfied
        // even though requireLiveEndpoints would refuse it.
        seedDoc("fk24-src");
        seedDoc("fk24-dst");
        assertThat(catalogRepo.deleteDocument(TENANT, "fk24-dst")).isEqualTo(1);
        assertThat(catalogRepo.upsertLink(TENANT, Map.of(
            "from_tumbler", "fk24-src", "to_tumbler", "fk24-dst",
            "link_type", "cites", "created_by", "test",
            "allow_dangling", true))).isTrue();
    }

    // ══════════════════════════════════════════════════════════════════════════
    // GROUP 3 — ON DELETE / ON UPDATE CASCADE (raw SQL, both direction and both sides)
    // ══════════════════════════════════════════════════════════════════════════

    @Test
    @Order(30)
    void hardDeleteDocument_cascadesLinksOnBothSides() throws Exception {
        seedDoc("fk30-a");
        seedDoc("fk30-b");
        assertThat(catalogRepo.upsertLink(TENANT, Map.of(
            "from_tumbler", "fk30-a", "to_tumbler", "fk30-b",
            "link_type", "cites", "created_by", "test"))).isTrue();
        assertThat(catalogRepo.upsertLink(TENANT, Map.of(
            "from_tumbler", "fk30-b", "to_tumbler", "fk30-a",
            "link_type", "relates", "created_by", "test"))).isTrue();

        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            int n = su.createStatement().executeUpdate(
                "DELETE FROM nexus.catalog_documents WHERE tenant_id = '" + TENANT + "' AND tumbler = 'fk30-a'");
            assertThat(n).isEqualTo(1);
        }

        assertThat(catalogRepo.linksFrom(TENANT, "fk30-a", (java.util.List<String>) null))
            .as("link with fk30-a as from_tumbler cascaded via fk_catalog_links_from_document").isEmpty();
        assertThat(catalogRepo.linksTo(TENANT, "fk30-a", (java.util.List<String>) null))
            .as("link with fk30-a as to_tumbler cascaded via fk_catalog_links_to_document").isEmpty();
    }

    @Test
    @Order(31)
    void tumblerRename_cascadesToLinksOnBothSides() throws Exception {
        // RDR-194 § D2 declares ON UPDATE CASCADE explicitly (unlike fk-001's
        // older FKs to the same referent, which predate this RDR and carry no
        // ON UPDATE clause). No production caller renames a tumbler today, but
        // the constraint's own stated contract is tested directly here rather
        // than left unverified.
        seedDoc("fk31-old");
        seedDoc("fk31-partner");
        assertThat(catalogRepo.upsertLink(TENANT, Map.of(
            "from_tumbler", "fk31-old", "to_tumbler", "fk31-partner",
            "link_type", "cites", "created_by", "test"))).isTrue();
        assertThat(catalogRepo.upsertLink(TENANT, Map.of(
            "from_tumbler", "fk31-partner", "to_tumbler", "fk31-old",
            "link_type", "relates", "created_by", "test"))).isTrue();

        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            int n = su.createStatement().executeUpdate(
                "UPDATE nexus.catalog_documents SET tumbler = 'fk31-new' "
                + "WHERE tenant_id = '" + TENANT + "' AND tumbler = 'fk31-old'");
            assertThat(n).isEqualTo(1);
        }

        assertThat(catalogRepo.linksFrom(TENANT, "fk31-new", (java.util.List<String>) null))
            .as("ON UPDATE CASCADE propagated the rename to from_tumbler").hasSize(1);
        assertThat(catalogRepo.linksTo(TENANT, "fk31-new", (java.util.List<String>) null))
            .as("ON UPDATE CASCADE propagated the rename to to_tumbler").hasSize(1);
        assertThat(catalogRepo.linksFrom(TENANT, "fk31-old", (java.util.List<String>) null))
            .as("no rows left under the old tumbler value").isEmpty();
    }

    // ══════════════════════════════════════════════════════════════════════════
    // GROUP 4 — import path (CatalogRepository.importLink / importLinksBatch /
    // doImportLink), nexus-tk070.p1 review fix 1 (RDR-194 § D2): the ETL import
    // path had NO SQLSTATE 23503 mapping at all before this fix — a dangling
    // row surfaced as the generic class-23 DB error instead of the same
    // dangling_endpoint 400 shape upsertLink produces.
    // ══════════════════════════════════════════════════════════════════════════

    @Test
    @Order(40)
    void importLink_liveEndpoints_succeeds() {
        seedDoc("fk40-src");
        seedDoc("fk40-dst");
        catalogRepo.importLink(TENANT, Map.of(
            "from_tumbler", "fk40-src", "to_tumbler", "fk40-dst",
            "link_type", "cites", "created_by", "test"));
        assertThat(catalogRepo.linksFrom(TENANT, "fk40-src", (java.util.List<String>) null)).hasSize(1);
    }

    @Test
    @Order(41)
    void importLink_tombstonedEndpoint_stillWrites() {
        // The narrowed (not disappeared) import-path contract (RDR-194 § D2):
        // a tombstoned endpoint still satisfies the FK -- the import path
        // legitimately writes edges for documents whose LIVE state it does
        // not control. Only an ABSENT endpoint is rejected (Order 42/43).
        seedDoc("fk41-src");
        seedDoc("fk41-dst");
        assertThat(catalogRepo.deleteDocument(TENANT, "fk41-dst")).isEqualTo(1);
        catalogRepo.importLink(TENANT, Map.of(
            "from_tumbler", "fk41-src", "to_tumbler", "fk41-dst",
            "link_type", "cites", "created_by", "test"));
        assertThat(catalogRepo.linksFrom(TENANT, "fk41-src", (java.util.List<String>) null)).hasSize(1);
    }

    @Test
    @Order(42)
    void importLink_nonexistentToTumbler_provokesTheRealFkViolation_danglingEndpointShape() {
        // Real PG constraint, not a mocked SQLState.
        seedDoc("fk42-src");
        var ex = assertThrows(
            CatalogRepository.DanglingEndpointException.class, () ->
            catalogRepo.importLink(TENANT, Map.of(
                "from_tumbler", "fk42-src", "to_tumbler", "fk42-nonexistent",
                "link_type", "cites", "created_by", "test")));
        assertThat(ex.missing()).containsExactly("to_tumbler");
        assertThat(catalogRepo.linksFrom(TENANT, "fk42-src", (java.util.List<String>) null)).isEmpty();
    }

    @Test
    @Order(43)
    void importLink_nonexistentFromTumbler_namesFromTumbler() {
        seedDoc("fk43-dst");
        var ex = assertThrows(
            CatalogRepository.DanglingEndpointException.class, () ->
            catalogRepo.importLink(TENANT, Map.of(
                "from_tumbler", "fk43-nonexistent", "to_tumbler", "fk43-dst",
                "link_type", "cites", "created_by", "test")));
        assertThat(ex.missing()).containsExactly("from_tumbler");
    }

    @Test
    @Order(44)
    void importLinksBatch_allValid_succeeds() {
        seedDoc("fk44-a");
        seedDoc("fk44-b");
        seedDoc("fk44-c");
        int n = catalogRepo.importLinksBatch(TENANT, java.util.List.of(
            Map.of("from_tumbler", "fk44-a", "to_tumbler", "fk44-b", "link_type", "cites", "created_by", "test"),
            Map.of("from_tumbler", "fk44-b", "to_tumbler", "fk44-c", "link_type", "cites", "created_by", "test")));
        assertThat(n).isEqualTo(2);
        assertThat(catalogRepo.linksFrom(TENANT, "fk44-a", (java.util.List<String>) null)).hasSize(1);
        assertThat(catalogRepo.linksFrom(TENANT, "fk44-b", (java.util.List<String>) null)).hasSize(1);
    }

    @Test
    @Order(45)
    void importLinksBatch_oneDanglingRow_abortsWholeChunk_noPartialWrite() {
        // No silent fallback / no per-row skipping (RDR-194 § D2, review fix 1):
        // a bad row aborts the WHOLE multi-row insert chunk -- even the OTHER
        // row in the same chunk, whose endpoints ARE live, does not write.
        seedDoc("fk45-a");
        seedDoc("fk45-b");
        var ex = assertThrows(
            CatalogRepository.DanglingEndpointException.class, () ->
            catalogRepo.importLinksBatch(TENANT, java.util.List.of(
                Map.of("from_tumbler", "fk45-a", "to_tumbler", "fk45-b", "link_type", "cites", "created_by", "test"),
                Map.of("from_tumbler", "fk45-a", "to_tumbler", "fk45-nonexistent", "link_type", "relates", "created_by", "test"))));
        assertThat(ex.missing()).containsExactly("to_tumbler");
        assertThat(ex.getMessage()).contains("fk45-nonexistent");
        assertThat(catalogRepo.linksFrom(TENANT, "fk45-a", (java.util.List<String>) null))
            .as("the whole chunk aborted -- even the live-endpoint row in the same chunk did not write")
            .isEmpty();
    }

    // ── helpers ──────────────────────────────────────────────────────────────

    private void seedDoc(String tumbler) {
        catalogRepo.upsertDocument(TENANT, Map.of(
            "tumbler", tumbler, "title", tumbler, "content_type", "paper",
            "corpus", "knowledge", "physical_collection", COLLECTION));
    }
}
