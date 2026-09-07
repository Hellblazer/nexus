// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.db.CatalogRepository;
import dev.nexus.service.db.Chash;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.vectors.PgVectorRepository;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.MethodOrderer;
import org.junit.jupiter.api.Order;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.junit.jupiter.api.TestMethodOrder;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.util.List;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-dkymw — {@code POST /v1/catalog/restore} and {@code GET
 * /v1/catalog/trash}, exercised at the {@link CatalogRepository} layer
 * ({@code restoreDocument}/{@code listTrash}, the exact methods {@code
 * CatalogHandler#handleRestore}/{@code #handleTrash} call into).
 *
 * <p>Sam's 2026-09-07 ruling on nexus-dkymw (RDR-106 Option A regression):
 * bless tombstones as the recovery story rather than resurrecting the
 * backup-before-delete machinery. This is the operator door nexus-xavu7
 * found missing — three sites told an operator to "restore the trashed
 * document(s)" with zero CLI/MCP/REST surface to do it, and {@code
 * nexus.document_restore} (catalog-003-soft-delete.xml, RDR-156 P1.2) had
 * existed with no caller anywhere in the stack since that changeset. This
 * suite pins the new caller pair.
 *
 * <p>Connects as {@code nexus_svc} directly (role-001-nexus-svc.xml,
 * password {@code nexus_svc_pass}) rather than minting a per-test service
 * role via {@code PgContainerHelper.bootstrapServiceRole} — {@code
 * nexus_svc} already carries EXECUTE on {@code nexus.purge_trash(interval)}
 * (catalog-003-soft-delete.xml changeset 7) and full DML on every {@code
 * nexus} table (grants-nexus-svc.xml), which the {@link
 * #restore_afterPurgeTrash_returnsZero_nothingLeftToRestore} case needs and
 * the test-role bootstrap's fixed grant set (TABLES/SEQUENCES only, no
 * FUNCTIONS) does not carry — avoiding a hand-rolled {@code GRANT EXECUTE}
 * statement keeps this file at zero raw SQL (RawSqlGateTest's test-tree
 * ratchet: a new file carries no {@code TEST_TREE_RAW_SQL_CEILING} entry and
 * must stay at zero).
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
@TestMethodOrder(MethodOrderer.OrderAnnotation.class)
class CatalogRestoreTrashTest {

    private static final String SVC_ROLE = "nexus_svc";
    private static final String SVC_PASS = "nexus_svc_pass";

    private static final String TENANT_A = "restore-tenant-a";
    private static final String TENANT_B = "restore-tenant-b";
    private static final String COLLECTION = "knowledge__restore-owner__minilm-l6-v2-384__v1";

    PostgreSQLContainer<?> pg;
    HikariDataSource svcDs;
    TenantScope tenantScope;
    CatalogRepository catalogRepo;
    PgVectorRepository vecRepo;

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();

        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
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
        var embedder = new PgVectorRepositoryContractTest.FakeEmbedder(384);
        vecRepo = new PgVectorRepository(tenantScope, embedder, embedder);
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    private static Map<String, Object> regDoc(String title, String filePath) {
        return Map.of("title", title, "content_type", "knowledge", "corpus", "knowledge",
                       "file_path", filePath, "physical_collection", COLLECTION);
    }

    @Test
    @Order(10)
    void restore_flipsTombstone_docReappearsInListAndShow() {
        String tumbler = catalogRepo.registerDocument(TENANT_A, "restore-basic", regDoc("Basic", "basic.md"));
        assertThat(catalogRepo.deleteDocument(TENANT_A, tumbler)).isEqualTo(1);
        assertThat(catalogRepo.getDocument(TENANT_A, tumbler)).isNull();
        assertThat(catalogRepo.listDocuments(TENANT_A, 200, 0))
            .extracting(d -> d.get("tumbler")).doesNotContain(tumbler);

        assertThat(catalogRepo.restoreDocument(TENANT_A, tumbler)).isEqualTo(1);

        assertThat(catalogRepo.getDocument(TENANT_A, tumbler)).isNotNull();
        assertThat(catalogRepo.listDocuments(TENANT_A, 200, 0))
            .extracting(d -> d.get("tumbler")).contains(tumbler);
    }

    @Test
    @Order(20)
    void restore_liveDocument_isNoOpReturnsZero() {
        String tumbler = catalogRepo.registerDocument(TENANT_A, "restore-live", regDoc("Live", "live.md"));
        assertThat(catalogRepo.restoreDocument(TENANT_A, tumbler)).isEqualTo(0);
        assertThat(catalogRepo.getDocument(TENANT_A, tumbler)).isNotNull();
    }

    @Test
    @Order(30)
    void restore_unknownTumbler_returnsZero() {
        assertThat(catalogRepo.restoreDocument(TENANT_A, "restore-tenant-a.nope.999")).isEqualTo(0);
    }

    /**
     * The manifest ROW is never deleted by a soft tombstone (fk-001 CASCADE does
     * not fire on an UPDATE) — {@link CatalogRepository#getManifest} hides it
     * from reads while the parent is tombstoned (nexus-mqd6t {@code
     * liveParentDoc} filter), not because the row is gone. Restoring the parent
     * makes the SAME row visible again, unchanged.
     */
    @Test
    @Order(40)
    void restore_manifestAndChunksSurviveIntact() {
        String tumbler = catalogRepo.registerDocument(
            TENANT_A, "restore-manifest", regDoc("Manifest", "manifest.md"));
        String chashHex = Chash.ofText("restore-manifest-chunk").toHex();
        vecRepo.upsertChunks(TENANT_A, COLLECTION,
            List.of(chashHex), List.of("chunk text"), List.of(Map.of()));
        catalogRepo.writeManifest(TENANT_A, tumbler, COLLECTION,
            List.of(Map.<String, Object>of("position", 0, "chash", chashHex, "chunk_index", 0)));
        assertThat(catalogRepo.getManifest(TENANT_A, tumbler)).hasSize(1);

        assertThat(catalogRepo.deleteDocument(TENANT_A, tumbler)).isEqualTo(1);
        assertThat(catalogRepo.getManifest(TENANT_A, tumbler))
            .as("tombstoned reads are invisible by design, not because the row was deleted")
            .isEmpty();

        assertThat(catalogRepo.restoreDocument(TENANT_A, tumbler)).isEqualTo(1);
        var manifest = catalogRepo.getManifest(TENANT_A, tumbler);
        assertThat(manifest).hasSize(1);
        assertThat(manifest.get(0).get("chash")).isEqualTo(chashHex);
    }

    /**
     * nexus-dkymw, Sam's 2026-09-07 second ruling: {@code chashesForCollection}
     * (the {@code nx t3 gc} / indexer-prune alive-set) must PROTECT a
     * tombstoned-but-not-yet-purged document's chashes, superseding
     * nexus-mqd6t's original immediate-exclusion fix for this one read.
     * Without this, {@code nx t3 gc}'s own {@code --orphan-window} clock
     * (independent of purge-trash's window) could reap the chunks of a
     * document tombstoned only seconds ago, and a later {@code nx catalog
     * restore} would resurrect an empty shell.
     */
    @Test
    @Order(45)
    void restore_chashesForCollection_protectsTombstonedDocUntilRestore() {
        String tumbler = catalogRepo.registerDocument(
            TENANT_A, "restore-gc-alive-set", regDoc("GC Alive Set", "gc-alive-set.md"));
        String chashHex = Chash.ofText("restore-gc-alive-set-chunk").toHex();
        vecRepo.upsertChunks(TENANT_A, COLLECTION,
            List.of(chashHex), List.of("chunk text"), List.of(Map.of()));
        catalogRepo.writeManifest(TENANT_A, tumbler, COLLECTION,
            List.of(Map.<String, Object>of("position", 0, "chash", chashHex, "chunk_index", 0)));

        assertThat(catalogRepo.chashesForCollection(TENANT_A, COLLECTION))
            .as("visible in the alive-set while live")
            .contains(chashHex);

        assertThat(catalogRepo.deleteDocument(TENANT_A, tumbler)).isEqualTo(1);

        assertThat(catalogRepo.chashesForCollection(TENANT_A, COLLECTION))
            .as("a tombstoned-but-not-yet-purged doc's chash must STAY in the T3 GC "
                + "alive-set — nx t3 gc must not reap it inside the restore window")
            .contains(chashHex);

        assertThat(catalogRepo.restoreDocument(TENANT_A, tumbler)).isEqualTo(1);

        var manifest = catalogRepo.getManifest(TENANT_A, tumbler);
        assertThat(manifest)
            .as("restore must not resurrect an empty shell — the chunk survived because "
                + "the alive-set protected it")
            .hasSize(1);
        assertThat(manifest.get(0).get("chash")).isEqualTo(chashHex);
    }

    /**
     * {@code older_than_days=0}: the aged-tombstone threshold is {@code NOW()}
     * itself, so a document tombstoned moments ago is already at/past it —
     * purges immediately, with no need to wait or backdate {@code deleted_at}.
     */
    @Test
    @Order(50)
    void restore_afterPurgeTrash_returnsZero_nothingLeftToRestore() {
        String tumbler = catalogRepo.registerDocument(
            TENANT_A, "restore-purged", regDoc("Purged", "purged.md"));
        assertThat(catalogRepo.deleteDocument(TENANT_A, tumbler)).isEqualTo(1);

        Map<String, Object> purged = catalogRepo.purgeTrash(TENANT_A, 0);
        assertThat(((Number) purged.get("documents_purged")).longValue()).isGreaterThanOrEqualTo(1L);

        assertThat(catalogRepo.restoreDocument(TENANT_A, tumbler)).isEqualTo(0);
        assertThat(catalogRepo.getDocument(TENANT_A, tumbler)).isNull();
    }

    @Test
    @Order(60)
    void trash_listsOnlyThisTenantsTombstones() {
        String tumblerA = catalogRepo.registerDocument(TENANT_A, "trash-a", regDoc("Trash A", "trash-a.md"));
        String tumblerB = catalogRepo.registerDocument(TENANT_B, "trash-b", regDoc("Trash B", "trash-b.md"));
        assertThat(catalogRepo.deleteDocument(TENANT_A, tumblerA)).isEqualTo(1);
        assertThat(catalogRepo.deleteDocument(TENANT_B, tumblerB)).isEqualTo(1);

        var trashA = catalogRepo.listTrash(TENANT_A, 200, 0);
        assertThat(trashA).extracting(d -> d.get("tumbler")).contains(tumblerA);
        assertThat(trashA).extracting(d -> d.get("tumbler")).doesNotContain(tumblerB);

        var trashB = catalogRepo.listTrash(TENANT_B, 200, 0);
        assertThat(trashB).extracting(d -> d.get("tumbler")).contains(tumblerB);
        assertThat(trashB).extracting(d -> d.get("tumbler")).doesNotContain(tumblerA);
    }

    @Test
    @Order(70)
    void trash_excludesLiveDocuments() {
        String tumbler = catalogRepo.registerDocument(
            TENANT_A, "trash-live-check", regDoc("Still live", "still-live.md"));
        var trashA = catalogRepo.listTrash(TENANT_A, 200, 0);
        assertThat(trashA).extracting(d -> d.get("tumbler")).doesNotContain(tumbler);
    }
}
